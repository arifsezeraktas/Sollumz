from mathutils import Vector
import numpy
from math import inf, sqrt
from mathutils import Vector, Quaternion


class StringHelper():

    @staticmethod
    def FixShaderName(name):
        if("." in name):
            name = name[:-4]
        return name


class ListHelper():

    @staticmethod
    def divide_list(list, d):
        result = []
        for item in list:
            answer = item / d
            result.append(answer)
        return result

    @staticmethod
    def float32_list(list):
        result = []
        for item in list:
            result.append(numpy.float32(item))
        return result


class VectorHelper():

    @staticmethod
    def subtract_from_vector(v, f):
        r = Vector((0, 0, 0))
        r.x = v.x - f
        r.y = v.y - f
        r.z = v.z - f
        return r

    @staticmethod
    def add_to_vector(v, f):
        r = Vector((0, 0, 0))
        r.x = v.x + f
        r.y = v.y + f
        r.z = v.z + f
        return r

    @staticmethod
    def get_min_vector(v, c):
        r = Vector((0, 0, 0))
        r.x = min(v.x, c.x)
        r.y = min(v.y, c.y)
        r.z = min(v.z, c.z)
        return r

    @staticmethod
    def get_max_vector(v, c):
        r = Vector((0, 0, 0))
        r.x = max(v.x, c.x)
        r.y = max(v.y, c.y)
        r.z = max(v.z, c.z)
        return r

    @staticmethod
    def get_min_vector_list(vecs):
        x = []
        y = []
        z = []
        for v in vecs:
            x.append(v[0])
            y.append(v[1])
            z.append(v[2])
        return Vector((min(x), min(y), min(z)))

    @staticmethod
    def get_max_vector_list(vecs):
        x = []
        y = []
        z = []
        for v in vecs:
            x.append(v[0])
            y.append(v[1])
            z.append(v[2])
        return Vector((max(x), max(y), max(z)))

    @staticmethod
    def add_vector_list(list1, list2):
        x = list1[0] + list2[0]
        y = list1[1] + list2[1]
        z = list1[2] + list2[2]
        return [x, y, z]

    @staticmethod
    def subtract_vector_list(list1, list2):
        x = list1[0] - list2[0]
        y = list1[1] - list2[1]
        z = list1[2] - list2[2]
        return [x, y, z]

    @staticmethod
    def multiple_vector_list(list, num):
        x = list[0] * num
        y = list[1] * num
        z = list[2] * num
        return [x, y, z]

    @staticmethod
    def get_vector_list_length(list):
        sx = list[0] ** 2
        sy = list[1] ** 2
        sz = list[2] ** 2
        length = (sx + sy + sz) ** 0.5
        return length

    @staticmethod
    def divide_vectors(a, b):
        return Vector((a.x/b.x, a.y/b.y, a.z/b.z))

    @staticmethod
    def get_closest_axis_point(axis, center, points):

        closest = None
        closestDist = inf

        for p in points:

            rel = (p - center).normalized()
            dist = (rel - axis).length

            if dist < closestDist:
                closest = p
                closestDist = dist

        return closest

    @staticmethod
    def get_distance_of_vectors(a, b):
        locx = b.x - a.x
        locy = b.y - a.y
        locz = b.z - a.z

        distance = sqrt((locx) ** 2 + (locy) ** 2 + (locz) ** 2)
        return distance

    @staticmethod
    def get_direction_of_vectors(a, b):
        direction = (a - b).normalized()
        axis_align = Vector((0.0, 0.0, 1.0))

        angle = axis_align.angle(direction)
        axis = axis_align.cross(direction)

        q = Quaternion(axis, angle)

        return q.to_euler("XYZ")
