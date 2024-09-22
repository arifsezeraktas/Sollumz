from bpy.types import (
    Mesh,
)
import bmesh
from enum import Enum
from typing import NamedTuple
from dataclasses import dataclass


class NavMeshAttr(str, Enum):
    # Polygons require 4-bytes to represent their attributes.
    # Even though 'INT' is 4-bytes, we only use the lower 2-bytes of 2 separate mesh attributes in case we need to
    # access them from shader nodes. Shader nodes cannot use integers and they are converted to 32-bit floats.
    # Limiting their values to 2-bytes we ensure we don't lose precision in the int -> float conversion.
    # See mesh_get/set_navmesh_poly_attributes for info on these attributes.
    POLY_DATA_0 = ".navmesh.poly_data0"
    POLY_DATA_1 = ".navmesh.poly_data1"

    EDGE_EXTERNAL_POLY = ".navmesh.edge_external_poly"

    @property
    def type(self):
        return "INT"

    @property
    def domain(self):
        match self:
            case NavMeshAttr.POLY_DATA_0 | NavMeshAttr.POLY_DATA_1:
                return "FACE"
            case NavMeshAttr.EDGE_EXTERNAL_POLY:
                return "EDGE"
            case _:
                assert False, f"Domain not set for navmesh attribute '{self}'"


def mesh_add_navmesh_attribute(mesh: Mesh, attr: NavMeshAttr):
    mesh.attributes.new(attr, attr.type, attr.domain)


def mesh_has_navmesh_attribute(mesh: Mesh, attr: NavMeshAttr) -> bool:
    return attr in mesh.attributes


class NavPolyCoverDirections(NamedTuple):
    dir0: bool  # __ +Y
    dir1: bool  # -X +Y
    dir2: bool  # -X __
    dir3: bool  # -X -Y
    dir4: bool  # __ -Y
    dir5: bool  # +X -Y
    dir6: bool  # +X __
    dir7: bool  # +X +Y


@dataclass
class NavPolyAttributes:
    is_small: bool
    is_large: bool
    is_pavement: bool
    is_in_shelter: bool
    is_too_steep_to_walk_on: bool
    is_water: bool
    is_near_car_node: bool
    is_interior: bool
    is_isolated: bool
    is_network_spawn_candidate: bool
    is_road: bool
    lies_along_edge: bool
    is_train_track: bool
    is_shallow_water: bool
    cover_directions: NavPolyCoverDirections
    audio_reverb_size: int  # 2 bits, 0..3
    audio_reverb_wet: int  # 2 bits, 0..3
    ped_density: int  # 3 bits, 0..7


def mesh_get_navmesh_poly_attributes(mesh: Mesh, poly_idx: int) -> NavPolyAttributes:
    if mesh.is_editmode:
        bm = bmesh.from_edit_mesh(mesh)
        bm.faces.ensure_lookup_table()

        data0_layer = bm.faces.layers.int[NavMeshAttr.POLY_DATA_0]
        data1_layer = bm.faces.layers.int[NavMeshAttr.POLY_DATA_1]

        data0 = 0 if data0_layer is None else bm.faces[poly_idx][data0_layer]
        data1 = 0 if data1_layer is None else bm.faces[poly_idx][data1_layer]
    else:
        data0_attr = mesh.attributes.get(NavMeshAttr.POLY_DATA_0, None)
        data1_attr = mesh.attributes.get(NavMeshAttr.POLY_DATA_1, None)

        data0 = 0 if data0_attr is None else data0_attr.data[poly_idx].value
        data1 = 0 if data1_attr is None else data1_attr.data[poly_idx].value

    flags0 = data0 & 0xFF
    flags1 = (data0 >> 8) & 0xFF
    flags2 = data1 & 0xFF
    flags3 = (data1 >> 8) & 0xFF

    return NavPolyAttributes(
        is_small=(flags0 & 1) != 0,
        is_large=(flags0 & 2) != 0,
        is_pavement=(flags0 & 4) != 0,
        is_in_shelter=(flags0 & 8) != 0,
        is_too_steep_to_walk_on=(flags0 & 64) != 0,
        is_water=(flags0 & 128) != 0,

        audio_reverb_size=flags1 & 3,
        audio_reverb_wet=(flags1 >> 2) & 3,
        is_near_car_node=(flags1 & 32) != 0,
        is_interior=(flags1 & 64) != 0,
        is_isolated=(flags1 & 128) != 0,

        is_network_spawn_candidate=(flags2 & 1) != 0,
        is_road=(flags2 & 2) != 0,
        lies_along_edge=(flags2 & 4) != 0,
        is_train_track=(flags2 & 8) != 0,
        is_shallow_water=(flags2 & 16) != 0,
        ped_density=(flags2 >> 5) & 7,

        cover_directions=NavPolyCoverDirections(
            *((flags3 & (1 << i)) != 0 for i in range(8))
        )
    )


def mesh_set_navmesh_poly_attributes(mesh: Mesh, poly_idx: int, poly_attrs: NavPolyAttributes):
    flags0 = 0
    flags0 |= 1 if poly_attrs.is_small else 0
    flags0 |= 2 if poly_attrs.is_large else 0
    flags0 |= 4 if poly_attrs.is_pavement else 0
    flags0 |= 8 if poly_attrs.is_in_shelter else 0
    flags0 |= 64 if poly_attrs.is_too_steep_to_walk_on else 0
    flags0 |= 128 if poly_attrs.is_water else 0

    flags1 = poly_attrs.audio_reverb_size & 3
    flags1 |= (poly_attrs.audio_reverb_wet & 3) << 2
    flags1 |= 32 if poly_attrs.is_near_car_node else 0
    flags1 |= 64 if poly_attrs.is_interior else 0
    flags1 |= 128 if poly_attrs.is_isolated else 0

    flags2 = 0
    flags2 |= 1 if poly_attrs.is_network_spawn_candidate else 0
    flags2 |= 2 if poly_attrs.is_road else 0
    flags2 |= 4 if poly_attrs.lies_along_edge else 0
    flags2 |= 8 if poly_attrs.is_train_track else 0
    flags2 |= 16 if poly_attrs.is_shallow_water else 0
    flags2 |= (poly_attrs.ped_density & 7) << 5

    flags3 = 0
    for i in range(8):
        flags3 |= (1 << i) if poly_attrs.cover_directions[i] else 0

    data0 = flags0 | (flags1 << 8)
    data1 = flags2 | (flags3 << 8)

    # TODO: add attributes if they don't exist in the mesh
    if mesh.is_editmode:
        bm = bmesh.from_edit_mesh(mesh)
        bm.faces.ensure_lookup_table()

        data0_layer = bm.faces.layers.int[NavMeshAttr.POLY_DATA_0]
        data1_layer = bm.faces.layers.int[NavMeshAttr.POLY_DATA_1]

        if data0_layer is not None:
            bm.faces[poly_idx][data0_layer] = data0
        if data1_layer is not None:
            bm.faces[poly_idx][data1_layer] = data1
    else:
        data0_attr = mesh.attributes.get(NavMeshAttr.POLY_DATA_0, None)
        data1_attr = mesh.attributes.get(NavMeshAttr.POLY_DATA_1, None)

        if data0_attr is not None:
            data0_attr.data[poly_idx].value = data0
        if data1_attr is not None:
            data1_attr.data[poly_idx].value = data1


#
# +++++++++++++ RAW POLY FLAGS +++++++++++++++
# (as of CW 30_dev47)
#
# Flag0
#   IsSmall = 1   # area < 2.0
#   IsLarge = 2   # area > 40.0
#   IsPavement = 4
#   InShelter = 8
#   Unused5 = 16
#   Unused6 = 32
#   TooSteepToWalkOn = 64
#   IsWater = 128
#
# Flag1
#   AudioProperties_Bit0 = 1
#   AudioProperties_Bit1 = 2
#   AudioProperties_Bit2 = 4
#   AudioProperties_Bit3 = 8
#   Unused4 = 16
#   IsNearCarNode = 32
#   IsInterior = 64
#   IsIsolated = 128
#
# Flag2
#   IsNetworkSpawnCandidate = 1
#   IsRoad = 2
#   LiesAlongEdge = 4
#   IsTrainTrack = 8
#   IsShallowWater = 16
#   PedDensity_Bit0 = 32  PedDensity 3 bits (0..7)
#   PedDensity_Bit1 = 64
#   PedDensity_Bit2 = 128
#
# Flag3
#   CoverDir0 = 1
#   CoverDir1 = 2
#   CoverDir2 = 4
#   CoverDir3 = 8
#   CoverDir4 = 16
#   CoverDir5 = 32
#   CoverDir6 = 64
#   CoverDir7 = 128
#
#
# +++++++++++++ POLY FLAGS +++++++++++++++
#
# User Controlled Values (or at least would be complicated to compute within Blender):
#   IsPavement
#   InShelter
#   TooSteepToWalkOn  # might be possible to calculate this one
#   IsWater
#   IsNearCarNode
#   IsInterior
#   IsIsolated
#   IsNetworkSpawnCandidate
#   IsRoad
#   IsTrainTrack
#   IsShallowWater
#   CoverDirs[8]
#   AudioProperties
#   PedDensity
#
#
# Calculated Values:
#   IsSmall       # area < 2.0
#   IsLarge       # area > 40.0
#   LiesAlongEdge # 2 contiguous vertices on the cell edge
#
