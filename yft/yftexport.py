import bpy
from typing import Optional, Tuple
from collections import defaultdict
from itertools import combinations
from mathutils import Matrix, Vector
from bpy_extras.mesh_utils import mesh_linked_triangles
from sys import float_info
import numpy as np

from ..ybn.ybnexport import create_composite_xml, get_scale_to_apply_to_bound
from ..cwxml.bound import Bound, BoundComposite
from ..cwxml.fragment import (
    Fragment, PhysicsLOD, Archetype, PhysicsChild, PhysicsGroup, Transform, Physics, BoneTransform, Window,
    GlassWindow, GlassWindows,
)
from ..cwxml.drawable import Bone, Drawable, ShaderGroup, VectorShaderParameter, VertexLayoutList
from ..cwxml.cloth import (
    EnvironmentCloth, VerletClothEdge
)
from ..cwxml.shader import ShaderManager
from ..tools.blenderhelper import get_evaluated_obj, remove_number_suffix, delete_hierarchy, get_child_of_bone
from ..tools.fragmenthelper import image_to_shattermap
from ..tools.meshhelper import flip_uvs, get_tangent_required
from ..tools.utils import prop_array_to_vector, reshape_mat_4x3, vector_inv, reshape_mat_3x4
from ..sollumz_helper import get_parent_inverse, get_sollumz_materials
from ..sollumz_properties import BOUND_TYPES, SollumType, MaterialType, LODLevel, VehiclePaintLayer
from ..sollumz_preferences import get_export_settings
from ..ybn.ybnexport import has_col_mats, bound_geom_has_mats
from ..ydr.ydrexport import create_drawable_xml, write_embedded_textures, get_bone_index, create_model_xml, append_model_xml, set_drawable_xml_extents
from ..ydr.lights import create_xml_lights
from .. import logger
from .properties import (
    LODProperties, FragArchetypeProperties, GroupProperties, PAINT_LAYER_VALUES,
    GroupFlagBit, get_glass_type_index,
    FragmentTemplateAsset,
)


def export_yft(frag_obj: bpy.types.Object, filepath: str) -> bool:
    export_settings = get_export_settings()
    frag_xml = create_fragment_xml(frag_obj, export_settings.apply_transforms)

    if frag_xml is None:
        return False

    if export_settings.export_non_hi:
        frag_xml.write_xml(filepath)
        write_embedded_textures(frag_obj, filepath)

    if export_settings.export_hi and has_hi_lods(frag_obj):
        hi_filepath = filepath.replace(".yft.xml", "_hi.yft.xml")

        hi_frag_xml = create_hi_frag_xml(frag_obj, frag_xml, export_settings.apply_transforms)
        hi_frag_xml.write_xml(hi_filepath)

        write_embedded_textures(frag_obj, hi_filepath)
        logger.info(f"Exported Very High LODs to '{hi_filepath}'")
    elif export_settings.export_hi and not export_settings.export_non_hi:
        logger.warning(f"Only Very High LODs selected to export but fragment '{frag_obj.name}' does not have Very High"
                       " LODs. Nothing was exported.")
        return False

    return True


def create_fragment_xml(frag_obj: bpy.types.Object, apply_transforms: bool = False):
    """Create an XML parsable Fragment object. Returns the XML object and the hi XML object (if hi lods are present)."""
    frag_xml = Fragment()
    frag_xml.name = f"pack:/{remove_number_suffix(frag_obj.name)}"

    if frag_obj.type != "ARMATURE":
        logger.warning(
            f"Failed to create Fragment XML: {frag_obj.name} must be an armature with a skeleton!")
        return

    set_frag_xml_properties(frag_obj, frag_xml)

    materials = get_sollumz_materials(frag_obj)
    drawable_xml = create_frag_drawable_xml(frag_obj, materials, apply_transforms)

    if drawable_xml is None:
        logger.warning(
            f"Failed to create Fragment XML: {frag_obj.name} has no Drawable!")
        return

    original_pose = frag_obj.data.pose_position
    frag_obj.data.pose_position = "REST"

    set_paint_layer_shader_params(materials, drawable_xml.shader_group)

    frag_xml.bounding_sphere_center = drawable_xml.bounding_sphere_center
    frag_xml.bounding_sphere_radius = drawable_xml.bounding_sphere_radius

    frag_xml.drawable = drawable_xml

    if frag_obj.data.bones:
        create_bone_transforms_xml(frag_xml)

    # Physics data doesn't do anything if no collisions are present and will cause crashes
    if frag_has_collisions(frag_obj) and frag_obj.data.bones:
        create_frag_physics_xml(frag_obj, frag_xml, materials)
        create_vehicle_windows_xml(frag_obj, frag_xml, materials)
    else:
        frag_xml.physics = None

    frag_xml.lights = create_xml_lights(frag_obj)

    env_cloth = create_frag_env_cloth(frag_obj, drawable_xml, materials)
    if env_cloth is not None:
        frag_xml.cloths = [env_cloth]  # cloths is an array but game only supports 1 cloth
        if frag_xml.drawable.is_empty:
            # If it doesn't have other drawable models other than the cloth one, we can remove the main drawable
            frag_xml.drawable = None
            frag_xml.bounding_sphere_center = env_cloth.drawable.bounding_sphere_center
            frag_xml.bounding_sphere_radius = env_cloth.drawable.bounding_sphere_radius

        if frag_xml.physics is None:
            frag_xml.physics = create_dummy_frag_physics_xml_for_cloth(frag_obj, frag_xml, materials)

    frag_obj.data.pose_position = original_pose

    return frag_xml


def create_frag_drawable_xml(frag_obj: bpy.types.Object, materials: list[bpy.types.Material], apply_transforms: bool = False):
    for obj in frag_obj.children:
        if obj.sollum_type != SollumType.DRAWABLE:
            continue

        drawable_xml = create_drawable_xml(
            obj, materials=materials, armature_obj=frag_obj, apply_transforms=apply_transforms)
        drawable_xml.name = "skel"

        return drawable_xml


def set_paint_layer_shader_params(materials: list[bpy.types.Material], shader_group: ShaderGroup):
    """Set matDiffuseColor shader params based off of paint layer selection (expects materials to be ordered by shader)"""
    for i, mat in enumerate(materials):
        paint_layer = mat.sollumz_paint_layer
        if paint_layer == VehiclePaintLayer.NOT_PAINTABLE:
            continue

        for param in shader_group.shaders[i].parameters:
            if not isinstance(param, VectorShaderParameter) or param.name != "matDiffuseColor":
                continue

            value = PAINT_LAYER_VALUES[paint_layer]
            param.x, param.y, param.z, param.w = (2, value, value, 0)


def create_hi_frag_xml(frag_obj: bpy.types.Object, frag_xml: Fragment, apply_transforms: bool = False):
    hi_obj = frag_obj.copy()
    hi_obj.name = f"{remove_number_suffix(hi_obj.name)}_hi"
    drawable_obj = None

    bpy.context.collection.objects.link(hi_obj)

    for child in frag_obj.children:
        if child.sollum_type == SollumType.DRAWABLE:
            drawable_obj = copy_hierarchy(child, hi_obj)
            drawable_obj.parent = hi_obj
            break

    if drawable_obj is not None:
        remove_non_hi_lods(drawable_obj)

    materials = get_sollumz_materials(hi_obj)
    hi_drawable = create_frag_drawable_xml(hi_obj, materials, apply_transforms)

    hi_frag_xml = Fragment()
    hi_frag_xml.__dict__ = frag_xml.__dict__.copy()
    hi_frag_xml.drawable = hi_drawable
    hi_frag_xml.vehicle_glass_windows = None

    if hi_frag_xml.physics is not None:
        # Physics children drawables are copied over from non-hi to the hi frag. Therefore, they have high, med and low
        # lods but we need the very high lods in the hi frag XML. Here we remove the existing lods and recreate the
        # drawables with the very high lods.
        # NOTE: we are doing a shallow copy, so we are modifying the original physics children here. This is fine
        # because`frag_xml` is not used after this call during YFT export, but if eventually we need to use it,
        # we should change to a deep copy.
        bones = hi_frag_xml.drawable.skeleton.bones
        child_meshes = get_child_meshes(hi_obj)
        for child_xml in hi_frag_xml.physics.lod1.children:
            drawable = child_xml.drawable
            drawable.drawable_models_high.clear()
            drawable.drawable_models_med.clear()
            drawable.drawable_models_low.clear()
            drawable.drawable_models_vlow.clear()

            bone_tag = child_xml.bone_tag
            bone_name = None
            for bone in bones:
                if bone.tag == bone_tag:
                    bone_name = bone.name
                    break

            mesh_objs = None
            if bone_name in child_meshes:
                mesh_objs = child_meshes[bone_name]

            create_phys_child_drawable(child_xml, materials, mesh_objs)

    delete_hierarchy(hi_obj)

    return hi_frag_xml


def copy_hierarchy(obj: bpy.types.Object, armature_obj: bpy.types.Object):
    obj_copy = obj.copy()

    bpy.context.collection.objects.link(obj_copy)

    for constraint in obj_copy.constraints:
        if constraint.type != "ARMATURE":
            continue

        for constraint_target in constraint.targets:
            constraint_target.target = armature_obj

    for modifier in obj_copy.modifiers:
        if modifier.type != "ARMATURE":
            continue

        modifier.object = armature_obj

    for child in obj.children:
        child_copy = copy_hierarchy(child, armature_obj)
        child_copy.parent = obj_copy

    return obj_copy


def remove_non_hi_lods(drawable_obj: bpy.types.Object):
    for model_obj in drawable_obj.children:
        if model_obj.sollum_type != SollumType.DRAWABLE_MODEL:
            continue

        lods = model_obj.sz_lods
        very_high_lod = lods.get_lod(LODLevel.VERYHIGH)

        if very_high_lod.mesh is None:
            bpy.data.objects.remove(model_obj)
            continue

        lods.get_lod(LODLevel.HIGH).mesh = very_high_lod.mesh
        lods.active_lod_level = LODLevel.HIGH

        for lod_level in LODLevel:
            if lod_level == LODLevel.HIGH:
                continue
            lod = lods.get_lod(lod_level)
            if lod.mesh is not None:
                lod.mesh = None


def copy_phys_xml(phys_xml: Physics, lod_props: LODProperties):
    new_phys_xml = Physics()
    lod_xml = PhysicsLOD("LOD1")
    new_phys_xml.lod1 = lod_xml
    new_phys_xml.lod2 = None
    new_phys_xml.lod3 = None

    lod_xml.archetype = phys_xml.lod1.archetype
    lod_xml.groups = phys_xml.lod1.groups
    lod_xml.archetype2 = None

    set_lod_xml_properties(lod_props, lod_xml)

    return new_phys_xml


def has_hi_lods(frag_obj: bpy.types.Object):
    for child in frag_obj.children_recursive:
        if child.sollum_type != SollumType.DRAWABLE_MODEL and not child.sollumz_is_physics_child_mesh:
            continue

        very_high_lod = child.sz_lods.get_lod(LODLevel.VERYHIGH)
        if very_high_lod.mesh is not None:
            return True

    return False


def sort_cols_and_children(lod_xml: PhysicsLOD):
    children_by_group: dict[int, list[int]] = defaultdict(list)

    bounds = lod_xml.archetype.bounds.children
    children = lod_xml.children

    if not bounds or not children:
        return

    for i, child in enumerate(children):
        children_by_group[child.group_index].append(i)

    children_by_group = dict(sorted(children_by_group.items()))

    # Map old indices to new ones
    indices: dict[int, int] = {}
    sorted_children: list[PhysicsChild] = []

    for group_index, children_indices in children_by_group.items():
        for child_index in children_indices:
            new_child_index = len(sorted_children)
            indices[child_index] = new_child_index

            sorted_children.append(children[child_index])

    lod_xml.children = sorted_children
    # Apply sorting to collisions
    sorted_collisions: list[Bound] = [None] * len(indices)

    for old_index, new_index in indices.items():
        sorted_collisions[new_index] = bounds[old_index]

    lod_xml.archetype.bounds.children = sorted_collisions


def frag_has_collisions(frag_obj: bpy.types.Object):
    return any(child.sollum_type == SollumType.BOUND_COMPOSITE for child in frag_obj.children)


def create_frag_physics_xml(frag_obj: bpy.types.Object, frag_xml: Fragment, materials: list[bpy.types.Material]):
    lod_props: LODProperties = frag_obj.fragment_properties.lod_properties
    drawable_xml = frag_xml.drawable

    lod_xml = create_phys_lod_xml(frag_xml.physics, lod_props)
    arch_xml = create_archetype_xml(lod_xml, frag_obj)
    col_obj_to_bound_index = dict()
    create_collision_xml(frag_obj, arch_xml, col_obj_to_bound_index)

    create_phys_xml_groups(frag_obj, lod_xml, frag_xml.glass_windows, materials)
    create_phys_child_xmls(frag_obj, lod_xml, drawable_xml.skeleton.bones, materials, col_obj_to_bound_index)

    calculate_group_masses(lod_xml)
    calculate_child_drawable_matrices(frag_xml)

    sort_cols_and_children(lod_xml)

    calculate_physics_lod_transforms(frag_xml)
    calculate_archetype_mass_inertia(lod_xml)
    calculate_physics_lod_inertia_limits(lod_xml)


def create_phys_lod_xml(phys_xml: Physics, lod_props: LODProperties):
    set_lod_xml_properties(lod_props, phys_xml.lod1)
    phys_xml.lod2 = None
    phys_xml.lod3 = None

    return phys_xml.lod1


def calculate_physics_lod_inertia_limits(lod_xml: PhysicsLOD):
    """Calculates the physics LOD smallest and largest angular inertia from its children."""
    phys_children = lod_xml.children
    inertia_values = [value for c in phys_children for value in c.inertia_tensor.xyz]
    largest_inertia = max(inertia_values)
    smallest_inertia = largest_inertia / 10000.0  # game assets always have same value as largest divided by 10000

    # unknown_14 = smallest angular inertia
    # unknown_18 = largest angular inertia
    lod_xml.unknown_14 = smallest_inertia
    lod_xml.unknown_18 = largest_inertia


def create_archetype_xml(lod_xml: PhysicsLOD, frag_obj: bpy.types.Object):
    archetype_props: FragArchetypeProperties = frag_obj.fragment_properties.lod_properties.archetype_properties

    set_archetype_xml_properties(archetype_props, lod_xml.archetype, remove_number_suffix(frag_obj.name))
    lod_xml.archetype2 = None

    return lod_xml.archetype


def calculate_archetype_mass_inertia(lod_xml: PhysicsLOD):
    """Set archetype mass and inertia based on children mass and bounds. Expects physics children and collisions to
    exist, and the physics LOD root CG to have already been calculted.
    """

    from ..shared.geometry import calculate_composite_inertia
    phys_children = lod_xml.children
    bounds = lod_xml.archetype.bounds.children
    masses = [child_xml.pristine_mass for child_xml in phys_children]
    inertias = [child_xml.inertia_tensor.xyz for child_xml in phys_children]
    cgs = [bound_xml.composite_transform.transposed() @ bound_xml.sphere_center for bound_xml in bounds]
    mass = sum(masses)
    inertia = calculate_composite_inertia(lod_xml.position_offset, cgs, masses, inertias)

    arch_xml = lod_xml.archetype
    arch_xml.mass = mass
    arch_xml.mass_inv = (1 / mass) if mass != 0 else 0
    arch_xml.inertia_tensor = inertia
    arch_xml.inertia_tensor_inv = vector_inv(inertia)


def create_collision_xml(
    frag_obj: bpy.types.Object,
    arch_xml: Archetype,
    col_obj_to_bound_index: dict[bpy.types.Object, int] = None
) -> BoundComposite:
    for child in frag_obj.children:
        if child.sollum_type != SollumType.BOUND_COMPOSITE:
            continue

        composite_xml = create_composite_xml(child, col_obj_to_bound_index)
        arch_xml.bounds = composite_xml

        composite_xml.unk_type = 2

        for bound_xml in composite_xml.children:
            bound_xml.unk_type = 2
        return composite_xml


def create_phys_xml_groups(
    frag_obj: bpy.types.Object,
    lod_xml: PhysicsLOD,
    glass_windows_xml: GlassWindows,
    materials: list[bpy.types.Material]
):
    group_ind_by_name: dict[str, int] = {}
    groups_by_bone: dict[int, list[PhysicsGroup]] = defaultdict(list)

    for bone in frag_obj.data.bones:
        if not bone.sollumz_use_physics:
            continue

        if not does_bone_have_collision(bone.name, frag_obj) and not does_bone_have_cloth(bone.name, frag_obj):
            logger.warning(
                f"Bone '{bone.name}' has physics enabled, but no associated collision! A collision must be linked to the bone for physics to work.")
            continue

        group_xml = PhysicsGroup()
        group_xml.name = bone.name
        bone_index = get_bone_index(frag_obj.data, bone)

        groups_by_bone[bone_index].append(group_xml)
        set_group_xml_properties(bone.group_properties, group_xml)

        if bone.group_properties.flags[GroupFlagBit.USE_GLASS_WINDOW]:
            add_frag_glass_window_xml(frag_obj, bone, materials, group_xml, glass_windows_xml)

    # Sort by bone index
    groups_by_bone = dict(sorted(groups_by_bone.items()))

    for groups in groups_by_bone.values():
        for group_xml in groups:
            i = len(group_ind_by_name)

            group_ind_by_name[group_xml.name] = i

    def get_group_parent_index(group_bone: bpy.types.Bone) -> int:
        """Returns parent group index or 255 if there is no parent."""
        parent_bone = group_bone.parent
        if parent_bone is None:
            return 255

        if not parent_bone.sollumz_use_physics or parent_bone.name not in group_ind_by_name:
            # Parent has no frag group, try with grandparent
            return get_group_parent_index(parent_bone)

        return group_ind_by_name[parent_bone.name]

    # Set group parent indices
    for bone_index, groups in groups_by_bone.items():
        parent_index = get_group_parent_index(frag_obj.data.bones[bone_index])

        for group_xml in groups:
            group_xml.parent_index = parent_index

            group_ind_by_name[group_xml.name] = len(lod_xml.groups)

            lod_xml.groups.append(group_xml)

    return lod_xml.groups


def does_bone_have_collision(bone_name: str, frag_obj: bpy.types.Object) -> bool:
    col_objs = [
        obj for obj in frag_obj.children_recursive if obj.sollum_type in BOUND_TYPES]

    for obj in col_objs:
        bone = get_child_of_bone(obj)

        if bone is not None and bone.name == bone_name:
            return True

    return False


def does_bone_have_cloth(bone_name: str, frag_obj: bpy.types.Object) -> bool:
    cloth_objs = get_frag_env_cloth_mesh_objects(frag_obj, silent=True)

    for obj in cloth_objs:
        bone = get_child_of_bone(obj)

        if bone is not None and bone.name == bone_name:
            return True

    return False


def calculate_group_masses(lod_xml: PhysicsLOD):
    """Calculate the mass of all groups in ``lod_xml`` based on child masses. Expects physics children to exist."""
    for child in lod_xml.children:
        lod_xml.groups[child.group_index].mass += child.pristine_mass


def calculate_physics_lod_transforms(frag_xml: Fragment):
    """Calculate ``frag_xml.physics.lod1.transforms``. A transformation matrix per physics child that represents
    the offset from the child collision bound to its link center of gravity (aka "link attachment"). A link is
    formed by physics groups that act as a rigid body together, a group with a joint creates a new link.
    Also calculates the physics LOD root CG offset.
    """

    lod_xml = frag_xml.physics.lod1
    bones_xml = frag_xml.drawable.skeleton.bones
    rotation_limits = frag_xml.drawable.joints.rotation_limits
    translation_limits = frag_xml.drawable.joints.translation_limits

    children_by_group: dict[PhysicsGroup, list[tuple[int, PhysicsChild]]] = defaultdict(list)
    for child_index, child in enumerate(lod_xml.children):
        group = lod_xml.groups[child.group_index]
        children_by_group[group].append((child_index, child))

    # Array of links (i.e. array of arrays of groups)
    links = [[]]  # the root link is at index 0
    link_index_by_group = [-1] * len(lod_xml.groups)

    # Determine the groups that form each link
    for group_index, group in enumerate(lod_xml.groups):
        link_index = 0  # by default add to root link

        if group.parent_index != 255:
            _, first_child = children_by_group[group][0]
            bone = next(b for b in bones_xml if b.tag == first_child.bone_tag)
            creates_new_link = (
                ("LimitRotation" in bone.flags and any(rl.bone_id == bone.tag for rl in rotation_limits)) or
                ("LimitTranslation" in bone.flags and any(tl.bone_id == bone.tag for tl in translation_limits))
            )
            if creates_new_link:
                # There is a joint, create a new link
                link_index = len(links)
                links.append([])
            else:
                # Add to link of parent group
                link_index = link_index_by_group[group.parent_index]

        links[link_index].append(group)
        link_index_by_group[group_index] = link_index

    # Calculate center of gravity of each link. This is the weighted mean of the center of gravity of all physics
    # children that form the link.
    links_center_of_gravity = [Vector((0.0, 0.0, 0.0)) for _ in range(len(links))]
    for link_index, groups in enumerate(links):
        link_total_mass = 0.0
        for group_index, group in enumerate(groups):
            for child_index_rel, (child_index, child) in enumerate(children_by_group[group]):
                bound = lod_xml.archetype.bounds.children[child_index]
                if bound is not None:
                    # sphere_center is the center of gravity
                    center = bound.composite_transform.transposed() @ bound.sphere_center
                else:
                    center = Vector((0.0, 0.0, 0.0))

                child_mass = child.pristine_mass
                links_center_of_gravity[link_index] += center * child_mass
                link_total_mass += child_mass

        links_center_of_gravity[link_index] /= link_total_mass

    # add the user-defined unbroken CG offset to the root CG offset
    links_center_of_gravity[0] += lod_xml.unknown_50

    lod_xml.position_offset = links_center_of_gravity[0]  # aka "root CG offset"
    lod_xml.unknown_40 = lod_xml.position_offset  # aka "original root CG offset", same as root CG offset in all game .yfts

    # Calculate child transforms (aka "link attachments", offset from bound to link CG)
    for child_index, child in enumerate(lod_xml.children):
        # print(f"#{child_index} ({child.bone_tag}) link_index={link_index_by_group[child.group_index]}")
        link_center = links_center_of_gravity[link_index_by_group[child.group_index]]
        bound = lod_xml.archetype.bounds.children[child_index]
        if bound is not None:
            offset = Matrix.Translation(-link_center) @ bound.composite_transform.transposed()
            offset.transpose()
        else:
            offset = Matrix.Identity(4)

        # It is a 3x4 matrix, so zero out the 4th column to be consistent with original matrices
        # (doesn't really matter but helps with equality checks in our tests)
        offset.col[3].zero()

        lod_xml.transforms.append(Transform("Item", offset))


def create_phys_child_xmls(
    frag_obj: bpy.types.Object,
    lod_xml: PhysicsLOD,
    bones_xml: list[Bone],
    materials: list[bpy.types.Material],
    col_obj_to_bound_index: dict[bpy.types.Object, int]
):
    """Creates the physics children XML objects for each collision object and adds them to ``lod_xml.children``.

    Additionally, makes sure that ``lod_xml.archetype.bounds.children`` order matches ``lod_xml.children`` order so
    the same indices can be used with both collections.
    """
    child_meshes = get_child_meshes(frag_obj)
    child_cols = get_child_cols(frag_obj)

    bound_index_to_child_index = []
    for bone_name, objs in child_cols.items():
        for obj in objs:
            child_index = len(lod_xml.children)
            bound_index = col_obj_to_bound_index[obj]
            bound_index_to_child_index.append((bound_index, child_index))

            bone: bpy.types.Bone = frag_obj.data.bones.get(bone_name)
            bone_index = get_bone_index(frag_obj.data, bone) or 0

            child_xml = PhysicsChild()
            child_xml.group_index = get_bone_group_index(lod_xml, bone_name)
            child_xml.pristine_mass = obj.child_properties.mass
            child_xml.damaged_mass = child_xml.pristine_mass
            child_xml.bone_tag = bones_xml[bone_index].tag
            child_xml.inertia_tensor = get_child_inertia(lod_xml.archetype, child_xml, bound_index)

            mesh_objs = None
            if bone_name in child_meshes:
                mesh_objs = child_meshes[bone_name]

            create_phys_child_drawable(child_xml, materials, mesh_objs)

            lod_xml.children.append(child_xml)

    # reorder bounds children based on physics children order
    bounds_children = lod_xml.archetype.bounds.children
    new_bounds_children = [None] * len(lod_xml.archetype.bounds.children)
    for bound_index, child_index in bound_index_to_child_index:
        new_bounds_children[child_index] = bounds_children[bound_index]
    lod_xml.archetype.bounds.children = new_bounds_children


def get_child_inertia(arch_xml: Archetype, child_xml: PhysicsChild, bound_index: int):
    if not arch_xml.bounds or bound_index >= len(arch_xml.bounds.children):
        return Vector()

    bound_xml = arch_xml.bounds.children[bound_index]
    inertia = bound_xml.inertia * child_xml.pristine_mass
    return Vector((inertia.x, inertia.y, inertia.z, bound_xml.volume * child_xml.pristine_mass))


def get_child_cols(frag_obj: bpy.types.Object):
    """Get collisions that are linked to a child. Returns a dict mapping each collision to a bone name."""
    child_cols_by_bone: dict[str, list[bpy.types.Object]] = defaultdict(list)

    for composite_obj in frag_obj.children:
        if composite_obj.sollum_type != SollumType.BOUND_COMPOSITE:
            continue

        for bound_obj in composite_obj.children:
            if not bound_obj.sollum_type in BOUND_TYPES:
                continue

            if (bound_obj.type == "MESH" and not has_col_mats(bound_obj)) or (bound_obj.type == "EMPTY" and not bound_geom_has_mats(bound_obj)):
                continue

            bone = get_child_of_bone(bound_obj)

            if bone is None or not bone.sollumz_use_physics:
                continue

            child_cols_by_bone[bone.name].append(bound_obj)

    return child_cols_by_bone


def get_child_meshes(frag_obj: bpy.types.Object):
    """Get meshes that are linked to a child. Returns a dict mapping child meshes to bone name."""
    child_meshes_by_bone: dict[str, list[bpy.types.Object]] = defaultdict(list)

    for drawable_obj in frag_obj.children:
        if drawable_obj.sollum_type != SollumType.DRAWABLE:
            continue

        for model_obj in drawable_obj.children:
            if model_obj.sollum_type != SollumType.DRAWABLE_MODEL or not model_obj.sollumz_is_physics_child_mesh:
                continue

            bone = get_child_of_bone(model_obj)

            if bone is None or not bone.sollumz_use_physics:
                continue

            child_meshes_by_bone[bone.name].append(model_obj)

    return child_meshes_by_bone


def get_bone_group_index(lod_xml: PhysicsLOD, bone_name: str):
    """Get index of group named ``bone_name`` (expects groups to have already been created in ``lod_xml``)."""
    for i, group in enumerate(lod_xml.groups):
        if group.name == bone_name:
            return i

    return -1


def create_child_mat_arrays(children: list[PhysicsChild]):
    """Create the matrix arrays for each child. This appears to be in the first child of multiple children that
    share the same group. Each matrix in the array is just the matrix for each child in that group."""
    group_inds = set(child.group_index for child in children)

    for i in group_inds:
        group_children = [
            child for child in children if child.group_index == i]

        if len(group_children) <= 1:
            continue

        first = group_children[0]

        for child in group_children[1:]:
            first.drawable.matrices.append(child.drawable.matrix)


def create_phys_child_drawable(child_xml: PhysicsChild, materials: list[bpy.types.Object], mesh_objs: Optional[list[bpy.types.Object]] = None):
    drawable_xml = child_xml.drawable
    drawable_xml.shader_group = None
    drawable_xml.skeleton = None
    drawable_xml.joints = None

    if not mesh_objs:
        return drawable_xml

    for obj in mesh_objs:
        scale = get_scale_to_apply_to_bound(obj)
        transforms_to_apply = Matrix.Diagonal(scale).to_4x4()

        lods = obj.sz_lods
        for lod_level in LODLevel:
            if lod_level == LODLevel.VERYHIGH:
                continue
            lod_mesh = lods.get_lod(lod_level).mesh
            if lod_mesh is None:
                continue

            model_xml = create_model_xml(obj, lod_level, materials, transforms_to_apply=transforms_to_apply)
            model_xml.bone_index = 0
            append_model_xml(drawable_xml, model_xml, lod_level)

    set_drawable_xml_extents(drawable_xml)

    return drawable_xml


def create_vehicle_windows_xml(frag_obj: bpy.types.Object, frag_xml: Fragment, materials: list[bpy.types.Material]):
    """Create all the vehicle windows for ``frag_xml``. Must be ran after the drawable and physics children have been created."""
    child_id_by_bone_tag: dict[str, int] = {
        c.bone_tag: i for i, c in enumerate(frag_xml.physics.lod1.children)}
    mat_ind_by_name: dict[str, int] = {
        mat.name: i for i, mat in enumerate(materials)}
    bones = frag_xml.drawable.skeleton.bones

    for obj in frag_obj.children_recursive:
        if not obj.child_properties.is_veh_window:
            continue

        bone = get_child_of_bone(obj)

        if bone is None or not bone.sollumz_use_physics:
            logger.warning(
                f"Vehicle window '{obj.name}' is not attached to a bone, or the attached bone does not have physics enabled! Attach the bone via an armature constraint.")
            continue

        bone_index = get_bone_index(frag_obj.data, bone)
        window_xml = Window()

        bone_tag = bones[bone_index].tag

        if bone_tag not in child_id_by_bone_tag:
            logger.warning(
                f"No physics child for the vehicle window '{obj.name}'!")
            continue

        window_xml.item_id = child_id_by_bone_tag[bone_tag]
        window_mat = obj.child_properties.window_mat

        if window_mat is None:
            logger.warning(
                f"Vehicle window '{obj.name}' has no material with the vehicle_vehglass shader!")
            continue

        if window_mat.name not in mat_ind_by_name:
            logger.warning(
                f"Vehicle window '{obj.name}' is using a vehicle_vehglass material '{window_mat.name}' that is not used in the Drawable! This material should be added to the mesh object attached to the bone '{bone.name}'.")
            continue

        set_veh_window_xml_properties(window_xml, obj)

        create_window_shattermap(obj, window_xml)

        shader_index = mat_ind_by_name[window_mat.name]
        window_xml.unk_ushort_1 = get_window_geometry_index(
            frag_xml.drawable, shader_index)

        frag_xml.vehicle_glass_windows.append(window_xml)

    frag_xml.vehicle_glass_windows = sorted(
        frag_xml.vehicle_glass_windows, key=lambda w: w.item_id)


def create_window_shattermap(col_obj: bpy.types.Object, window_xml: Window):
    """Create window shattermap (if it exists) and calculate projection"""
    shattermap_obj = get_shattermap_obj(col_obj)

    if shattermap_obj is None:
        return

    shattermap_img = find_shattermap_image(shattermap_obj)

    if shattermap_img is not None:
        window_xml.shattermap = image_to_shattermap(shattermap_img)
        window_xml.projection_matrix = calculate_shattermap_projection(shattermap_obj, shattermap_img)


def set_veh_window_xml_properties(window_xml: Window, window_obj: bpy.types.Object):
    window_xml.unk_float_17 = window_obj.vehicle_window_properties.data_min
    window_xml.unk_float_18 = window_obj.vehicle_window_properties.data_max
    window_xml.cracks_texture_tiling = window_obj.vehicle_window_properties.cracks_texture_tiling


def calculate_shattermap_projection(obj: bpy.types.Object, img: bpy.types.Image):
    mesh = obj.data

    v1 = Vector()
    v2 = Vector()
    v3 = Vector()

    # Get three corner vectors
    for loop in mesh.loops:
        uv = mesh.uv_layers[0].data[loop.index].uv
        vert_pos = mesh.vertices[loop.vertex_index].co

        if uv.x == 0 and uv.y == 1:
            v1 = vert_pos
        elif uv.x == 1 and uv.y == 1:
            v2 = vert_pos
        elif uv.x == 0 and uv.y == 0:
            v3 = vert_pos

    resx = img.size[0]
    resy = img.size[1]
    thickness = 0.01

    edge1 = (v2 - v1) / resx
    edge2 = (v3 - v1) / resy
    edge3 = edge1.normalized().cross(edge2.normalized()) * thickness

    matrix = Matrix()
    matrix[0] = edge1.x, edge2.x, edge3.x, v1.x
    matrix[1] = edge1.y, edge2.y, edge3.y, v1.y
    matrix[2] = edge1.z, edge2.z, edge3.z, v1.z

    # Create projection matrix relative to parent
    parent_inverse = get_parent_inverse(obj)
    matrix = parent_inverse @ obj.matrix_world @ matrix

    try:
        matrix.invert()
    except ValueError:
        logger.warning(
            f"Failed to create shattermap projection matrix for '{obj.name}'. Ensure the object is a flat plane with 4 vertices.")
        return Matrix()

    return matrix


def get_shattermap_obj(col_obj: bpy.types.Object) -> Optional[bpy.types.Object]:
    for child in col_obj.children:
        if child.sollum_type == SollumType.SHATTERMAP:
            return child


def find_shattermap_image(obj: bpy.types.Object) -> Optional[bpy.types.Image]:
    """Find shattermap material on ``obj`` and get the image attached to the base color node."""
    for mat in obj.data.materials:
        if mat.sollum_type != MaterialType.SHATTER_MAP:
            continue

        for node in mat.node_tree.nodes:
            if not isinstance(node, bpy.types.ShaderNodeTexImage):
                continue

            return node.image


def get_window_material(obj: bpy.types.Object) -> Optional[bpy.types.Material]:
    """Get first material with a vehicle_vehglass shader."""
    for mat in obj.data.materials:
        if "vehicle_vehglass" in mat.shader_properties.name:
            return mat


def get_window_geometry_index(drawable_xml: Drawable, window_shader_index: int):
    """Get index of the geometry using the window material."""
    for dmodel_xml in drawable_xml.drawable_models_high:
        for (index, geometry) in enumerate(dmodel_xml.geometries):
            if geometry.shader_index != window_shader_index:
                continue

            return index

    return 0


def create_bone_transforms_xml(frag_xml: Fragment):
    def get_bone_transforms(bone: Bone):
        return Matrix.LocRotScale(bone.translation, bone.rotation, bone.scale)

    bones: list[Bone] = frag_xml.drawable.skeleton.bones

    for bone in bones:

        transforms = get_bone_transforms(bone)

        if bone.parent_index != -1:
            parent_transforms = frag_xml.bones_transforms[bone.parent_index].value
            transforms = parent_transforms @ transforms

        # Reshape to 3x4
        transforms_reshaped = reshape_mat_3x4(transforms)

        frag_xml.bones_transforms.append(
            BoneTransform("Item", transforms_reshaped))


def calculate_child_drawable_matrices(frag_xml: Fragment):
    """Calculate the matrix for each physics child Drawable from bone transforms
    and composite transforms. Each matrix represents the transformation of the
    child relative to the bone."""
    bone_transforms = frag_xml.bones_transforms
    bones = frag_xml.drawable.skeleton.bones
    lod_xml = frag_xml.physics.lod1
    collisions = lod_xml.archetype.bounds.children

    bone_transform_by_tag: dict[str, Matrix] = {
        b.tag: bone_transforms[i].value for i, b in enumerate(bones)}

    for i, child in enumerate(lod_xml.children):
        bone_transform = bone_transform_by_tag[child.bone_tag]
        col = collisions[i]

        bone_inv = bone_transform.to_4x4().inverted()

        matrix = col.composite_transform @ bone_inv.transposed()
        child.drawable.matrix = reshape_mat_4x3(matrix)

    create_child_mat_arrays(lod_xml.children)


def set_lod_xml_properties(lod_props: LODProperties, lod_xml: PhysicsLOD):
    lod_xml.unknown_1c = lod_props.min_move_force
    lod_xml.unknown_50 = prop_array_to_vector(lod_props.unbroken_cg_offset)
    lod_xml.damping_linear_c = prop_array_to_vector(lod_props.damping_linear_c)
    lod_xml.damping_linear_v = prop_array_to_vector(lod_props.damping_linear_v)
    lod_xml.damping_linear_v2 = prop_array_to_vector(lod_props.damping_linear_v2)
    lod_xml.damping_angular_c = prop_array_to_vector(lod_props.damping_angular_c)
    lod_xml.damping_angular_v = prop_array_to_vector(lod_props.damping_angular_v)
    lod_xml.damping_angular_v2 = prop_array_to_vector(lod_props.damping_angular_v2)


def set_archetype_xml_properties(archetype_props: FragArchetypeProperties, arch_xml: Archetype, frag_name: str):
    arch_xml.name = frag_name
    arch_xml.unknown_48 = archetype_props.gravity_factor
    arch_xml.unknown_4c = archetype_props.max_speed
    arch_xml.unknown_50 = archetype_props.max_ang_speed
    arch_xml.unknown_54 = archetype_props.buoyancy_factor


def set_group_xml_properties(group_props: GroupProperties, group_xml: PhysicsGroup):
    group_xml.glass_window_index = 0
    group_xml.glass_flags = 0
    for i in range(len(group_props.flags)):
        group_xml.glass_flags |= (1 << i) if group_props.flags[i] else 0
    group_xml.strength = group_props.strength
    group_xml.force_transmission_scale_up = group_props.force_transmission_scale_up
    group_xml.force_transmission_scale_down = group_props.force_transmission_scale_down
    group_xml.joint_stiffness = group_props.joint_stiffness
    group_xml.min_soft_angle_1 = group_props.min_soft_angle_1
    group_xml.max_soft_angle_1 = group_props.max_soft_angle_1
    group_xml.max_soft_angle_2 = group_props.max_soft_angle_2
    group_xml.max_soft_angle_3 = group_props.max_soft_angle_3
    group_xml.rotation_speed = group_props.rotation_speed
    group_xml.rotation_strength = group_props.rotation_strength
    group_xml.restoring_strength = group_props.restoring_strength
    group_xml.restoring_max_torque = group_props.restoring_max_torque
    group_xml.latch_strength = group_props.latch_strength
    group_xml.min_damage_force = group_props.min_damage_force
    group_xml.damage_health = group_props.damage_health
    group_xml.unk_float_5c = group_props.weapon_health
    group_xml.unk_float_60 = group_props.weapon_scale
    group_xml.unk_float_64 = group_props.vehicle_scale
    group_xml.unk_float_68 = group_props.ped_scale
    group_xml.unk_float_6c = group_props.ragdoll_scale
    group_xml.unk_float_70 = group_props.explosion_scale
    group_xml.unk_float_74 = group_props.object_scale
    group_xml.unk_float_78 = group_props.ped_inv_mass_scale
    group_xml.unk_float_a8 = group_props.melee_scale


def set_frag_xml_properties(frag_obj: bpy.types.Object, frag_xml: Fragment):
    frag_xml.unknown_b0 = 0  # estimated cache sizes, these are set by the game when the fragCacheEntry is initialized
    frag_xml.unknown_b8 = 0
    frag_xml.unknown_bc = 0
    frag_xml.unknown_c0 = (FragmentTemplateAsset[frag_obj.fragment_properties.template_asset] & 0xFF) << 8
    frag_xml.unknown_c4 = frag_obj.fragment_properties.flags
    frag_xml.unknown_cc = frag_obj.fragment_properties.unbroken_elasticity
    frag_xml.gravity_factor = frag_obj.fragment_properties.gravity_factor
    frag_xml.buoyancy_factor = frag_obj.fragment_properties.buoyancy_factor


def add_frag_glass_window_xml(
    frag_obj: bpy.types.Object,
    glass_window_bone: bpy.types.Bone,
    materials: list[bpy.types.Material],
    group_xml: PhysicsGroup,
    glass_windows_xml: GlassWindows
):
    mesh_obj, col_obj = get_frag_glass_window_mesh_and_col(frag_obj, glass_window_bone)
    if mesh_obj is None or col_obj is None:
        logger.warning(f"Glass window '{group_xml.name}' is missing the mesh and/or collision. Skipping...")
        return

    group_xml.glass_window_index = len(glass_windows_xml)

    glass_type = glass_window_bone.group_properties.glass_type
    glass_type_index = get_glass_type_index(glass_type)

    glass_window_xml = GlassWindow()
    glass_window_xml.flags = glass_type_index & 0xFF
    glass_window_xml.layout = VertexLayoutList(type="GTAV4",
                                               value=["Position", "Normal", "Colour0", "TexCoord0", "TexCoord1"])

    glass_windows_xml.append(glass_window_xml)

    # calculate properties from the mesh
    mesh_obj_eval = get_evaluated_obj(mesh_obj)
    mesh = mesh_obj_eval.to_mesh()
    mesh_planes = mesh_linked_triangles(mesh)
    if len(mesh_planes) != 2:
        logger.warning(f"Glass window '{group_xml.name}' requires 2 separate planes in mesh.")
        if len(mesh_planes) < 2:
            return  # need at least 2 planes to continue

    plane_a, plane_b = mesh_planes[:2]
    if len(plane_a) != 2 or len(plane_b) != 2:
        logger.warning(f"Glass window '{group_xml.name}' mesh planes need to be made up of 2 triangles each.")
        if len(plane_a) < 2 or len(plane_b) < 2:
            return  # need at least 2 tris in each plane to continue

    normals = (plane_a[0].normal, plane_a[1].normal, plane_b[0].normal, plane_b[1].normal)
    if any(a.cross(b).length_squared > float_info.epsilon for a, b in combinations(normals, 2)):
        logger.warning(f"Glass window '{group_xml.name}' mesh planes are not parallel.")

    # calculate UV min/max (unused by the game)
    uvs = np.empty((len(mesh.loops), 2), dtype=np.float32)
    mesh.uv_layers[0].data.foreach_get("uv", uvs.ravel())
    flip_uvs(uvs)
    uv_min = uvs.min(axis=0)
    uv_max = uvs.max(axis=0)

    # calculate glass thickness
    center_a = (plane_a[0].center + plane_a[1].center) * 0.5
    center_b = (plane_b[0].center + plane_b[1].center) * 0.5
    thickness = (center_a - center_b).length

    # calculate tangent (unused by the game)
    tangent = normals[0].cross(Vector((0.0, 0.0, 1.0)))

    # calculate projection matrix
    #   get plane vertices sorted by normalized UV distance to (0, 0)
    plane_loops = {loop for tri in plane_a for loop in tri.loops}
    plane_loops = sorted(plane_loops, key=lambda loop: np.linalg.norm((uvs[loop] - uv_min) / (uv_max - uv_min)))
    plane_verts_and_uvs = [(mesh.loops[loop].vertex_index, uvs[loop]) for loop in plane_loops]

    #   get vertices needed to build the projection (top-left, top-right and bottom-left)
    v0_idx, v0_uv = plane_verts_and_uvs[0]  # vertex at UV min
    v1_idx = next(vert_idx for vert_idx, uv in plane_verts_and_uvs
                  if abs(uv[0] - v0_uv[0]) > abs(uv[1] - v0_uv[1]))  # vertex to the right of v0
    v2_idx = next(vert_idx for vert_idx, uv in plane_verts_and_uvs
                  if abs(uv[1] - v0_uv[1]) > abs(uv[0] - v0_uv[0]))  # vertex below v0
    v0 = mesh.vertices[v0_idx].co
    v1 = mesh.vertices[v1_idx].co
    v2 = mesh.vertices[v2_idx].co

    #   build projection and apply object transform
    transform = get_parent_inverse(mesh_obj_eval) @ mesh_obj_eval.matrix_world
    transform.invert()
    T = v0 @ transform
    V = (v1 - v0) @ transform
    U = (v2 - v0) @ transform
    projection = Matrix((T, V, U))

    # calculate shader index
    material = mesh.materials[0] if len(mesh.materials) > 0 else None
    if material is not None:
        shader_index = next((i for i, mat in enumerate(materials) if mat == material.original), -1)
    else:
        shader_index = -1

    if shader_index == -1:
        logger.warning(f"Glass window '{group_xml.name}' mesh is missing a material.")

    # calculate bounds offset front/back
    world_transform = mesh_obj_eval.matrix_world
    center_a_world = world_transform @ center_a
    normal_a_world = normals[0].copy()
    normal_a_world.rotate(world_transform)
    bounds_offset_front, bounds_offset_back = calc_frag_glass_window_bounds_offset(col_obj,
                                                                                   center_a_world, normal_a_world)

    mesh_obj_eval.to_mesh_clear()

    glass_window_xml.flags |= (shader_index & 0xFF) << 8
    glass_window_xml.projection_matrix = projection
    glass_window_xml.unk_float_13, glass_window_xml.unk_float_14 = uv_min
    glass_window_xml.unk_float_15, glass_window_xml.unk_float_16 = uv_max
    glass_window_xml.thickness = thickness
    glass_window_xml.unk_float_18 = bounds_offset_front
    glass_window_xml.unk_float_19 = bounds_offset_back
    glass_window_xml.tangent = tangent


def get_frag_glass_window_mesh_and_col(
    frag_obj: bpy.types.Object,
    glass_window_bone: bpy.types.Bone
) -> Tuple[Optional[bpy.types.Object], Optional[bpy.types.Object]]:
    """Finds the mesh and collision object for the glass window bone.
    Returns tuple (mesh_obj, col_obj)
    """
    mesh_obj = None
    col_obj = None
    for obj in frag_obj.children_recursive:
        if obj.sollum_type != SollumType.DRAWABLE_MODEL and obj.sollum_type not in BOUND_TYPES:
            continue

        parent_bone = get_child_of_bone(obj)
        if parent_bone != glass_window_bone:
            continue

        if obj.sollum_type == SollumType.DRAWABLE_MODEL:
            mesh_obj = obj
        else:
            col_obj = obj

        if mesh_obj is not None and col_obj is not None:
            break

    return mesh_obj, col_obj


def calc_frag_glass_window_bounds_offset(
    col_obj: bpy.types.Object,
    point: Vector,
    point_normal: Vector
) -> Tuple[float, float]:
    """Calculates the front and back offset from ``point`` to ``col_obj`` bound box.
    ``point`` and ``point_normal`` must be in world space.

    Returns tuple (offset_front, offset_back).
    """
    from mathutils.geometry import distance_point_to_plane, normal

    def _get_plane(a: Vector, b: Vector, c: Vector, d: Vector):
        plane_no = normal((a, b, c))
        plane_co = a
        return plane_co, plane_no

    bbs = [col_obj.matrix_world @ Vector(corner) for corner in col_obj.bound_box]

    # bound box corners:
    #  [0] = (min.x, min.y, min.z)
    #  [1] = (min.x, min.y, max.z)
    #  [2] = (min.x, max.y, max.z)
    #  [3] = (min.x, max.y, min.z)
    #  [4] = (max.x, min.y, min.z)
    #  [5] = (max.x, min.y, max.z)
    #  [6] = (max.x, max.y, max.z)
    #  [7] = (max.x, max.y, min.z)
    plane_points = (
        (bbs[4], bbs[3], bbs[0], bbs[7]),  # bottom
        (bbs[1], bbs[2], bbs[5], bbs[7]),  # top
        (bbs[2], bbs[1], bbs[0], bbs[3]),  # left
        (bbs[4], bbs[5], bbs[6], bbs[7]),  # right
        (bbs[0], bbs[1], bbs[4], bbs[5]),  # front
        (bbs[2], bbs[3], bbs[6], bbs[7]),  # back
    )
    planes = [_get_plane(Vector(a), Vector(b), Vector(c), Vector(d)) for a, b, c, d in plane_points]

    offset_front = 0.0
    offset_front_dot = 0.0
    offset_back = 0.0
    offset_back_dot = 0.0
    for plane_co, plane_no in planes:
        d = point_normal.dot(plane_no)
        if d > offset_front_dot:  # positive dot product is the plane with same normal as the point (in front)
            offset_front_dot = d
            offset_front = distance_point_to_plane(point, plane_co, plane_no)
        elif d < offset_back_dot:  # negative dot product is the plane with opposite normal as the point (behind)
            offset_back_dot = d
            offset_back = distance_point_to_plane(point, plane_co, plane_no)

    return offset_front, offset_back


def get_frag_env_cloth_mesh_objects(frag_obj: bpy.types.Object, silent: bool = False) -> list[bpy.types.Object]:
    """Returns a list of mesh objects that use a cloth material in the fragment. Warns the user if a mesh has a cloth
    material but also other materials or multiple cloth materials.
    """
    mesh_objs = []
    for obj in frag_obj.children_recursive:
        if obj.sollum_type != SollumType.DRAWABLE_MODEL or obj.type != "MESH":
            continue

        mesh = obj.data
        num_cloth_materials = 0
        num_other_materials = 0
        for material in mesh.materials:
            shader_def = ShaderManager.find_shader(material.shader_properties.filename)
            is_cloth_material = shader_def is not None and shader_def.is_cloth
            if is_cloth_material:
                num_cloth_materials += 1
            else:
                num_other_materials += 1

        match (num_cloth_materials, num_other_materials):
            case (1, 0):
                # Only cloth
                mesh_objs.append(obj)
            case (0, _):
                # Not cloth, ignore
                pass
            case (_, 0):
                # More than one cloth material, warning
                if not silent:
                    logger.warning(
                        f"Drawable model '{obj.name}' has multiple cloth materials! "
                        f"This is not supported, only a single cloth material per mesh is supported."
                    )
            case (_, _):
                # Multiple materials including cloth, warning
                if not silent:
                    logger.warning(
                        f"Drawable model '{obj.name}' has a cloth material along with other materials! "
                        f"This is not supported, only a single cloth material per mesh is supported."
                    )

    return mesh_objs


def create_frag_env_cloth(frag_obj: bpy.types.Object, drawable_xml: Drawable, materials: list[bpy.types.Material]) -> Optional[EnvironmentCloth]:
    cloth_objs = get_frag_env_cloth_mesh_objects(frag_obj)
    if not cloth_objs:
        return None

    cloth_obj = cloth_objs[0]
    if len(cloth_objs) > 1:
        other_cloth_objs = cloth_objs[1:]
        other_cloth_objs_names = [f"'{o.name}'" for o in other_cloth_objs]
        other_cloth_objs_names = ", ".join(other_cloth_objs_names)
        logger.warning(
            f"Fragment '{frag_obj.name}' has multiple cloth drawable models! "
            f"Only a single cloth per fragment is supported, drawable model '{cloth_obj.name}' will be used.\n"
            f"The following drawable models will be ignored: {other_cloth_objs_names}."
        )

    from .cloth import CLOTH_MAX_VERTICES, mesh_get_cloth_attribute_values, ClothAttr

    env_cloth = EnvironmentCloth()
    # env_cloth.flags = ValueProperty("Unknown78", 1)
    # env_cloth.user_data = TextListProperty("UnknownData")
    # env_cloth.tuning = ClothInstanceTuning()
    # env_cloth.drawable = Drawable()
    cloth_obj_eval = get_evaluated_obj(cloth_obj)
    cloth_mesh = cloth_obj_eval.to_mesh()
    cloth_mesh.calc_loop_triangles()

    num_vertices = len(cloth_mesh.vertices)
    if num_vertices > CLOTH_MAX_VERTICES:
        logger.error(
            f"Fragment '{frag_obj.name}' has cloth with too many vertices! "
            f"The maximum is {CLOTH_MAX_VERTICES} vertices but drawable model '{cloth_obj.name}' has "
            f"{num_vertices} vertices.\n"
            f"Cloth won't be exported!"
        )
        return None

    pinned = np.array(mesh_get_cloth_attribute_values(cloth_mesh, ClothAttr.PINNED)) != 0
    num_pinned = np.sum(pinned)

    mesh_to_cloth_vertex_map = [None] * num_vertices
    cloth_to_mesh_vertex_map = [None] * num_vertices
    vertices = [None] * num_vertices
    cloth_pin_index = 0
    cloth_unpin_index = num_pinned
    for v in cloth_mesh.vertices:
        vi = None
        if pinned[v.index]:
            vi = cloth_pin_index
            cloth_pin_index += 1
        else:
            vi = cloth_unpin_index
            cloth_unpin_index += 1

        vertices[vi] = Vector(v.co)
        mesh_to_cloth_vertex_map[v.index] = vi
        cloth_to_mesh_vertex_map[vi] = v.index
        # print(f"v {v.index}  = {v.co}")

    triangles = cloth_mesh.loop_triangles

    controller = env_cloth.controller
    controller.name = remove_number_suffix(frag_obj.name) + "_cloth"
    controller.morph_controller.map_data_high.poly_count = len(triangles)
    controller.flags = 3  # owns morph controller + owns bridge
    bridge = controller.bridge
    bridge.vertex_count_high = num_vertices
    pin_radius = mesh_get_cloth_attribute_values(cloth_mesh, ClothAttr.PIN_RADIUS)
    vertex_weights = mesh_get_cloth_attribute_values(cloth_mesh, ClothAttr.VERTEX_WEIGHT)
    inflation_scale = mesh_get_cloth_attribute_values(cloth_mesh, ClothAttr.INFLATION_SCALE)
    bridge.pin_radius_high = [pin_radius[mi] for mi in cloth_to_mesh_vertex_map]
    bridge.vertex_weights_high = [vertex_weights[mi] for mi in cloth_to_mesh_vertex_map]
    bridge.inflation_scale_high = [inflation_scale[mi] for mi in cloth_to_mesh_vertex_map]
    bridge.display_map_high = [-1] * num_vertices
    bridge.pinnable_list = [0] * int(np.ceil(num_vertices / 32)) # just need to allocate space for the pinnable list



    edges = []
    edges_added = set()
    for tri in triangles:
        v0, v1, v2 = tri.vertices
        for edge_v0, edge_v1 in ((v0, v1), (v1, v2), (v2, v0)):
            if (edge_v0, edge_v1) in edges_added or (edge_v1, edge_v0) in edges_added:
                continue

            if pinned[edge_v0] and pinned[edge_v1]:
                continue

            verlet_edge = VerletClothEdge()
            verlet_edge.vertex0 = mesh_to_cloth_vertex_map[edge_v0]
            verlet_edge.vertex1 = mesh_to_cloth_vertex_map[edge_v1]
            verlet_edge.length_sqr = Vector(vertices[verlet_edge.vertex0] - vertices[verlet_edge.vertex1]).length_squared
            verlet_edge.weight0 = 0.0 if pinned[edge_v0] else 1.0 if pinned[edge_v1] else 0.5
            verlet_edge.compression_weight = 0.25 # TODO: compression_weight
            edges.append(verlet_edge)
            edges_added.add((edge_v0, edge_v1))

    del edges_added


    # sort edges such that no vertex is repeated within chunks of 8 edges
    # fairly inefficient algorithm ahead, works for now
    edge_buckets = [[] for _ in range(len(edges) * 4)]
    last_bucket_index = -1
    MAX_EDGES_IN_BUCKET = 8
    for e in edges:
        for bucket_index, bucket in enumerate(edge_buckets):
            if len(bucket) >= MAX_EDGES_IN_BUCKET:
                continue

            can_add_to_bucket = True
            for edge_in_bucket in bucket:
                if (e.vertex0 == edge_in_bucket.vertex0 or e.vertex0 == edge_in_bucket.vertex1 or
                    e.vertex1 == edge_in_bucket.vertex0 or e.vertex1 == edge_in_bucket.vertex1):
                    can_add_to_bucket = False
                    break

            if can_add_to_bucket:
                bucket.append(e)
                if bucket_index > last_bucket_index:
                    last_bucket_index = bucket_index
                break

    new_edges = []
    for bucket_index, bucket in enumerate(edge_buckets):
        if bucket_index > last_bucket_index:
            break

        for i in range(MAX_EDGES_IN_BUCKET):
            if i < len(bucket):
                new_edges.append(bucket[i])
            else:
                # insert dummy edge
                verlet_edge = VerletClothEdge()
                verlet_edge.vertex0 = 0
                verlet_edge.vertex1 = 0
                verlet_edge.length_sqr = 1e8
                verlet_edge.weight0 = 0.0
                verlet_edge.compression_weight = 0.0
                new_edges.append(verlet_edge)

    edges = new_edges

    del edge_buckets
    del last_bucket_index
    del new_edges


    verlet = controller.cloth_high  # TODO: other lods
    verlet.vertex_positions = vertices
    # verlet.vertex_normals = ...  # TODO: cloth vertex normals, when should be exported? they are not always there
    verlet.bb_min = Vector(np.min(vertices, axis=0))
    verlet.bb_max = Vector(np.max(vertices, axis=0))
    verlet.switch_distance_up = 500.0  # TODO: switch distance? think it is only needed with multiple lods
    verlet.switch_distance_down = 0.0
    verlet.flags = 0  # TODO: flags
    verlet.dynamic_pin_list_size = 6  # TODO: what determines the dynamic pin list size?
    verlet.cloth_weight = 1.0  # TODO: cloth weight
    verlet.edges = edges
    verlet.pinned_vertices_count = num_pinned  # TODO: pinned vertices
    # verlet.custom_edges = ...  # TODO: custom edges

    # eds = []
    # for e in verlet.edges:
    #     v0 = verlet.vertex_positions[e.vertex0]
    #     v1 = verlet.vertex_positions[e.vertex1]
    #     if v1.length < v0.length:
    #         v0, v1 = v1, v0
    #     eds.append((v0, v1))
    #
    # eds.sort(key=lambda v: (v[0] + v[1]).length)
    # for v0, v1 in eds:
    #     print(f"{v0.x:.3f}, {v0.y:.3f}, {v0.z:.3f} -- {v1.x:.3f}, {v1.y:.3f}, {v1.z:.3f}")

    # Remove elements for other LODs for now
    controller.cloth_med = None
    controller.cloth_low = None
    controller.cloth_vlow = None
    controller.morph_controller.map_data_high.morph_map_high_weights = None
    controller.morph_controller.map_data_high.morph_map_high_vertex_index = None
    controller.morph_controller.map_data_high.morph_map_high_index0 = None
    controller.morph_controller.map_data_high.morph_map_high_index1 = None
    controller.morph_controller.map_data_high.morph_map_high_index2 = None
    controller.morph_controller.map_data_high.morph_map_med_weights = None
    controller.morph_controller.map_data_high.morph_map_med_vertex_index = None
    controller.morph_controller.map_data_high.morph_map_med_index0 = None
    controller.morph_controller.map_data_high.morph_map_med_index1 = None
    controller.morph_controller.map_data_high.morph_map_med_index2 = None
    controller.morph_controller.map_data_high.morph_map_low_weights = None
    controller.morph_controller.map_data_high.morph_map_low_vertex_index = None
    controller.morph_controller.map_data_high.morph_map_low_index0 = None
    controller.morph_controller.map_data_high.morph_map_low_index1 = None
    controller.morph_controller.map_data_high.morph_map_low_index2 = None
    controller.morph_controller.map_data_high.morph_map_vlow_weights = None
    controller.morph_controller.map_data_high.morph_map_vlow_vertex_index = None
    controller.morph_controller.map_data_high.morph_map_vlow_index0 = None
    controller.morph_controller.map_data_high.morph_map_vlow_index1 = None
    controller.morph_controller.map_data_high.morph_map_vlow_index2 = None
    controller.morph_controller.map_data_high.index_map_high = None
    controller.morph_controller.map_data_high.index_map_med = None
    controller.morph_controller.map_data_high.index_map_low = None
    controller.morph_controller.map_data_high.index_map_vlow = None
    controller.morph_controller.map_data_med = None
    controller.morph_controller.map_data_low = None
    controller.morph_controller.map_data_vlow = None
    # bridge.vertex_count_med = None
    # bridge.vertex_count_low = None
    # bridge.vertex_count_vlow = None
    bridge.pin_radius_med = None
    bridge.pin_radius_low = None
    bridge.pin_radius_vlow = None
    bridge.vertex_weights_med = None
    bridge.vertex_weights_low = None
    bridge.vertex_weights_vlow = None
    bridge.inflation_scale_med = None
    bridge.inflation_scale_low = None
    bridge.inflation_scale_vlow = None
    bridge.display_map_med = None
    bridge.display_map_low = None
    bridge.display_map_vlow = None

    env_cloth.tuning = None

    cloth_drawable_xml = env_cloth.drawable
    cloth_drawable_xml.name = "skel"
    cloth_drawable_xml.shader_group = drawable_xml.shader_group
    cloth_drawable_xml.skeleton = drawable_xml.skeleton
    cloth_drawable_xml.joints = drawable_xml.joints

    scale = get_scale_to_apply_to_bound(cloth_obj)
    transforms_to_apply = Matrix.Diagonal(scale).to_4x4()

    # TODO: lods
    model_xml = create_model_xml(cloth_obj, LODLevel.HIGH, materials, transforms_to_apply=transforms_to_apply)

    bone = get_child_of_bone(cloth_obj)
    if bone is None:
        logger.error(
            f"Fragment cloth '{cloth_obj.name}' is not attached to a bone! "
            "Attach it to a bone via a Copy Transforms constraint."
        )
        return None

    model_xml.bone_index = get_bone_index(frag_obj.data, bone)

    append_model_xml(cloth_drawable_xml, model_xml, LODLevel.HIGH)

    set_drawable_xml_extents(cloth_drawable_xml)

    # Cloth require a different FVF than the default one
    model_xml.geometries[0].vertex_buffer.get_element("layout").type = "GTAV2" if get_tangent_required(cloth_obj_eval.data.materials[0]) else "GTAV3"

    from ..ydr.ydrexport import set_drawable_xml_flags, set_drawable_xml_properties
    set_drawable_xml_flags(cloth_drawable_xml)
    assert cloth_obj.parent.sollum_type == SollumType.DRAWABLE
    set_drawable_xml_properties(cloth_obj.parent, cloth_drawable_xml)

    # Compute display map
    bridge.display_map_high = [-1] * len(model_xml.geometries[0].vertex_buffer.data["Position"])
    for mesh_vertex_index, mesh_vertex in enumerate(model_xml.geometries[0].vertex_buffer.data["Position"]):
        matching_cloth_vertex_index = None
        for cloth_vertex_index, cloth_vertex in enumerate(verlet.vertex_positions):
            if np.allclose(mesh_vertex, cloth_vertex, atol=1e-3):
                matching_cloth_vertex_index = cloth_vertex_index
                break

        assert matching_cloth_vertex_index is not None

        bridge.display_map_high[mesh_vertex_index] = matching_cloth_vertex_index

    cloth_obj_eval.to_mesh_clear()

    return env_cloth


def create_dummy_frag_physics_xml_for_cloth(frag_obj: bpy.types.Object, frag_xml: Fragment, materials: list[bpy.types.Material]) -> Physics:
    dummy_physics = Physics()
    lod_props: LODProperties = frag_obj.fragment_properties.lod_properties

    lod_xml = create_phys_lod_xml(dummy_physics, lod_props)
    arch_xml = create_archetype_xml(lod_xml, frag_obj)

    create_phys_xml_groups(frag_obj, lod_xml, frag_xml.glass_windows, materials)
    arch_xml.bounds.volume = 1
    arch_xml.bounds.inertia = Vector((1.0, 1.0, 1.0))
    arch_xml.bounds.sphere_radius = frag_xml.bounding_sphere_radius
    lod_xml.groups[0].mass = 1

    child_xml = PhysicsChild()
    child_xml.group_index = 0
    child_xml.pristine_mass = lod_xml.groups[0].mass
    child_xml.damaged_mass = child_xml.pristine_mass
    child_xml.bone_tag = 0
    child_xml.inertia_tensor = Vector((0.0, 0.0, 0.0, 0.0))

    create_phys_child_drawable(child_xml, materials, None)

    lod_xml.children.append(child_xml)

    return dummy_physics
