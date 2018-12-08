from django.db import models
from django.test import TestCase
from rest_framework import serializers

from drf_writable_nested import mixins


#####################
# Generic Serializer
#####################
class Child(models.Model):
    name = models.TextField()


class ChildSerializer(mixins.GetOrCreateNestedSerializerMixin, serializers.ModelSerializer):
    class Meta:
        model = Child
        fields = '__all__'


class GenericParentSerializer(mixins.RelatedSaveMixin, serializers.Serializer):
    class Meta:
        fields = '__all__'
    # source of a 1:many relationship
    child = ChildSerializer()

    def save(self):
        # "container only", no create logic
        pass


##################
# Direct Relation
##################
class Parent(models.Model):
    child = models.ForeignKey(Child, on_delete=models.CASCADE)


class ParentMany(models.Model):
    children = models.ManyToManyField(Child)


class ParentSerializer(mixins.RelatedSaveMixin, serializers.ModelSerializer):
    class Meta:
        model = Parent
        fields = '__all__'
    # source of a 1:many relationship
    child = ChildSerializer()


class ParentManySerializer(mixins.RelatedSaveMixin, serializers.ModelSerializer):
    class Meta:
        model = ParentMany
        fields = '__all__'
    # source of a m2m relationship
    children = ChildSerializer(many=True)


###################
# Reverse Relation
###################
class ReverseParent(models.Model):
    pass


class ReverseChild(models.Model):
    name = models.TextField()
    parent = models.ForeignKey(ReverseParent, on_delete=models.CASCADE, related_name='children')


class ReverseManyParent(models.Model):
    pass


class ReverseManyChild(models.Model):
    name = models.TextField()
    parent = models.ManyToManyField(ReverseManyParent, related_name='children')


class ReverseChildSerializer(mixins.GetOrCreateNestedSerializerMixin, serializers.ModelSerializer):
    class Meta:
        model = ReverseChild
        fields = '__all__'


class ReverseParentSerializer(mixins.RelatedSaveMixin, serializers.ModelSerializer):
    class Meta:
        model = ReverseParent
        fields = '__all__'
    # target of a 1:many relationship
    children = ReverseChildSerializer(many=True)


class ReverseManyParentSerializer(mixins.RelatedSaveMixin, serializers.ModelSerializer):
    class Meta:
        model = ReverseManyParent
        fields = '__all__'
    # target of a m2m relationship
    children = ReverseChildSerializer(many=True)


class WritableNestedModelSerializerTest(TestCase):

    def test_generic_nested_create(self):
        data = {
            "child": {
                "name": "test",
            }
        }

        serializer = GenericParentSerializer(data=data)
        valid = serializer.is_valid()
        self.assertTrue(
            valid,
            "Serializer should have been valid:  {}".format(serializer.errors)
        )
        serializer.save()

    def test_direct_nested_create(self):
        data = {
            "child": {
                "name": "test",
            }
        }

        serializer = ParentSerializer(data=data)
        valid = serializer.is_valid()
        self.assertTrue(
            valid,
            "Serializer should have been valid:  {}".format(serializer.errors)
        )
        serializer.save()

    def test_direct_many_nested_create(self):
        data = {
            "children": [{
                "name": "test",
            }]
        }

        serializer = ParentManySerializer(data=data)
        valid = serializer.is_valid()
        self.assertTrue(
            valid,
            "Serializer should have been valid:  {}".format(serializer.errors)
        )
        serializer.save()

    def test_reverse_nested_create(self):
        data = {
            "children": [{
                "name": "test",
            }]
        }

        serializer = ReverseParentSerializer(data=data)
        valid = serializer.is_valid()
        self.assertTrue(
            valid,
            "Serializer should have been valid:  {}".format(serializer.errors)
        )
        serializer.save()

    def test_reverse_many_nested_create(self):
        data = {
            "children": [{
                "name": "test",
            }]
        }

        serializer = ReverseManyParentSerializer(data=data)
        valid = serializer.is_valid()
        self.assertTrue(
            valid,
            "Serializer should have been valid:  {}".format(serializer.errors)
        )
        serializer.save()


###################
# 3-Layer Relation
###################
class GrandParent(models.Model):
    child = models.ForeignKey(Parent, on_delete=models.CASCADE)


class NestedParentSerializer(mixins.GetOrCreateNestedSerializerMixin, serializers.ModelSerializer):
    class Meta:
        model = Parent
        fields = '__all__'
    # source of a 1:many relationship
    child = ChildSerializer()


class GrandParentSerializer(mixins.RelatedSaveMixin, serializers.ModelSerializer):
    class Meta:
        model = GrandParent
        fields = '__all__'
    # source of a 1:many relationship
    child = NestedParentSerializer()


class NestedWritableNestedModelSerializerTest(TestCase):

    def test_direct_nested_create(self):
        data = {
            "child": {
                "child": {
                    "name": "test",
                }
            }
        }

        serializer = GrandParentSerializer(data=data)
        valid = serializer.is_valid()
        self.assertTrue(
            valid,
            "Serializer should have been valid:  {}".format(serializer.errors)
        )
        serializer.save()

        self.assertEqual(
            1,
            GrandParent.objects.count(),
        )

        self.assertEqual(
            1,
            Parent.objects.count(),
        )

        self.assertEqual(
            1,
            Child.objects.count(),
        )
