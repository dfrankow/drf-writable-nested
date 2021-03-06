# -*- coding: utf-8 -*-
from collections import OrderedDict, defaultdict

from django.contrib.contenttypes.fields import GenericRelation
from django.contrib.contenttypes.models import ContentType
from django.db.models import ProtectedError, FieldDoesNotExist, ObjectDoesNotExist
from django.db.models.fields.related import ForeignObjectRel
from django.utils.translation import ugettext_lazy as _
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.fields import empty
from rest_framework.validators import UniqueValidator, UniqueTogetherValidator
# permit writable nested serializers
serializers.raise_errors_on_nested_writes = lambda a, b, c: None


class BaseNestedModelSerializer(serializers.ModelSerializer):
    def _extract_relations(self, validated_data):
        reverse_relations = OrderedDict()
        relations = OrderedDict()

        # Remove related fields from validated data for future manipulations
        for field_name, field in self.fields.items():
            if field.read_only:
                continue
            try:
                related_field, direct = self._get_related_field(field)
            except FieldDoesNotExist:
                continue

            if isinstance(field, serializers.ListSerializer) and \
                    isinstance(field.child, serializers.ModelSerializer):
                if field.source not in validated_data:
                    # Skip field if field is not required
                    continue

                validated_data.pop(field.source)

                reverse_relations[field_name] = (
                    related_field, field.child, field.source)

            if isinstance(field, serializers.ModelSerializer):
                if field.source not in validated_data:
                    # Skip field if field is not required
                    continue

                if validated_data.get(field.source) is None:
                    if direct:
                        # Don't process null value for direct relations
                        # Native create/update processes these values
                        continue

                validated_data.pop(field.source)
                # Reversed one-to-one looks like direct foreign keys but they
                # are reverse relations
                if direct:
                    relations[field_name] = (field, field.source)
                else:
                    reverse_relations[field_name] = (
                        related_field, field, field.source)

        return relations, reverse_relations

    def _get_related_field(self, field):
        model_class = self.Meta.model

        try:
            related_field = model_class._meta.get_field(field.source)
        except FieldDoesNotExist:
            # If `related_name` is not set, field name does not include
            # `_set` -> remove it and check again
            default_postfix = '_set'
            if field.source.endswith(default_postfix):
                related_field = model_class._meta.get_field(
                    field.source[:-len(default_postfix)])
            else:
                raise

        if isinstance(related_field, ForeignObjectRel):
            return related_field.field, False
        return related_field, True

    def _get_serializer_for_field(self, field, **kwargs):
        kwargs.update({
            'context': self.context,
            'partial': self.partial if kwargs.get('instance') else False,
        })
        return field.__class__(**kwargs)

    def _get_generic_lookup(self, instance, related_field):
        return {
            related_field.content_type_field_name:
                ContentType.objects.get_for_model(instance),
            related_field.object_id_field_name: instance.pk,
        }

    def _get_related_pk(self, data, model_class):
        pk = data.get('pk') or data.get(model_class._meta.pk.attname)

        if pk:
            return str(pk)

        return None

    def _extract_related_pks(self, field, related_data):
        model_class = field.Meta.model
        pk_list = []
        for d in filter(None, related_data):
            pk = self._get_related_pk(d, model_class)
            if pk:
                pk_list.append(pk)

        return pk_list

    def _prefetch_related_instances(self, field, related_data):
        model_class = field.Meta.model
        pk_list = self._extract_related_pks(field, related_data)

        instances = {
            str(related_instance.pk): related_instance
            for related_instance in model_class.objects.filter(
                pk__in=pk_list
            )
        }

        return instances

    def update_or_create_reverse_relations(self, instance, reverse_relations):
        # Update or create reverse relations:
        # many-to-one, many-to-many, reversed one-to-one
        for field_name, (related_field, field, field_source) in \
                reverse_relations.items():

            # Skip processing for empty data or not-specified field.
            # The field can be defined in validated_data but isn't defined
            # in initial_data (for example, if multipart form data used)
            related_data = self.get_initial().get(field_name, None)
            if related_data is None:
                continue

            if related_field.one_to_one:
                # If an object already exists, fill in the pk so
                # we don't try to duplicate it
                pk_name = field.Meta.model._meta.pk.attname
                if pk_name not in related_data and 'pk' in related_data:
                    pk_name = 'pk'
                if pk_name not in related_data:
                    related_instance = getattr(instance, field_source, None)
                    if related_instance:
                        related_data[pk_name] = related_instance.pk

                # Expand to array of one item for one-to-one for uniformity
                related_data = [related_data]

            instances = self._prefetch_related_instances(field, related_data)

            save_kwargs = self._get_save_kwargs(field_name)
            if isinstance(related_field, GenericRelation):
                save_kwargs.update(
                    self._get_generic_lookup(instance, related_field),
                )
            elif not related_field.many_to_many:
                save_kwargs[related_field.name] = instance

            new_related_instances = []
            errors = []
            for data in related_data:
                obj = instances.get(
                    self._get_related_pk(data, field.Meta.model)
                )
                serializer = self._get_serializer_for_field(
                    field,
                    instance=obj,
                    data=data,
                )
                try:
                    serializer.is_valid(raise_exception=True)
                    related_instance = serializer.save(**save_kwargs)
                    data['pk'] = related_instance.pk
                    new_related_instances.append(related_instance)
                    errors.append({})
                except ValidationError as exc:
                    errors.append(exc.detail)

            if any(errors):
                if related_field.one_to_one:
                    raise ValidationError({field_name: errors[0]})
                else:
                    raise ValidationError({field_name: errors})

            if related_field.many_to_many:
                # Add m2m instances to through model via add
                m2m_manager = getattr(instance, field_source)
                m2m_manager.add(*new_related_instances)

    def update_or_create_direct_relations(self, attrs, relations):
        for field_name, (field, field_source) in relations.items():
            obj = None
            data = self.get_initial()[field_name]
            model_class = field.Meta.model
            pk = self._get_related_pk(data, model_class)
            if pk:
                obj = model_class.objects.filter(
                    pk=pk,
                ).first()
            serializer = self._get_serializer_for_field(
                field,
                instance=obj,
                data=data,
            )

            try:
                serializer.is_valid(raise_exception=True)
                attrs[field_source] = serializer.save(
                    **self._get_save_kwargs(field_name)
                )
            except ValidationError as exc:
                raise ValidationError({field_name: exc.detail})

    def save(self, **kwargs):
        self._save_kwargs = defaultdict(dict, kwargs)

        return super(BaseNestedModelSerializer, self).save(**kwargs)

    def _get_save_kwargs(self, field_name):
        save_kwargs = self._save_kwargs[field_name]
        if not isinstance(save_kwargs, dict):
            raise TypeError(
                _("Arguments to nested serializer's `save` must be dict's")
            )

        return save_kwargs


class NestedCreateMixin(BaseNestedModelSerializer):
    """
    Adds nested create feature
    """
    def create(self, validated_data):
        relations, reverse_relations = self._extract_relations(validated_data)

        # Create or update direct relations (foreign key, one-to-one)
        self.update_or_create_direct_relations(
            validated_data,
            relations,
        )

        # Create instance
        instance = super(NestedCreateMixin, self).create(validated_data)

        self.update_or_create_reverse_relations(instance, reverse_relations)

        return instance


class NestedUpdateMixin(BaseNestedModelSerializer):
    """
    Adds update nested feature
    """
    default_error_messages = {
        'cannot_delete_protected': _(
            "Cannot delete {instances} because "
            "protected relation exists")
    }

    def update(self, instance, validated_data):
        relations, reverse_relations = self._extract_relations(validated_data)

        # Create or update direct relations (foreign key, one-to-one)
        self.update_or_create_direct_relations(
            validated_data,
            relations,
        )

        # Update instance
        instance = super(NestedUpdateMixin, self).update(
            instance,
            validated_data,
        )
        self.update_or_create_reverse_relations(instance, reverse_relations)
        self.delete_reverse_relations_if_need(instance, reverse_relations)
        return instance

    def delete_reverse_relations_if_need(self, instance, reverse_relations):
        # Reverse `reverse_relations` for correct delete priority
        reverse_relations = OrderedDict(
            reversed(list(reverse_relations.items())))

        # Delete instances which is missed in data
        for field_name, (related_field, field, field_source) in \
                reverse_relations.items():
            model_class = field.Meta.model

            related_data = self.get_initial()[field_name]
            # Expand to array of one item for one-to-one for uniformity
            if related_field.one_to_one:
                related_data = [related_data]

            # M2M relation can be as direct or as reverse. For direct relation
            # we should use reverse relation name
            if related_field.many_to_many and \
                    not isinstance(related_field, ForeignObjectRel):
                related_field_lookup = {
                    related_field.remote_field.name: instance,
                }
            elif isinstance(related_field, GenericRelation):
                related_field_lookup = \
                    self._get_generic_lookup(instance, related_field)
            else:
                related_field_lookup = {
                    related_field.name: instance,
                }

            current_ids = self._extract_related_pks(field, related_data)

            try:
                pks_to_delete = list(
                    model_class.objects.filter(
                        **related_field_lookup
                    ).exclude(
                        pk__in=current_ids
                    ).values_list('pk', flat=True)
                )

                if related_field.many_to_many:
                    # Remove relations from m2m table
                    m2m_manager = getattr(instance, field_source)
                    m2m_manager.remove(*pks_to_delete)
                else:
                    model_class.objects.filter(pk__in=pks_to_delete).delete()

            except ProtectedError as e:
                instances = e.args[1]
                self.fail('cannot_delete_protected', instances=", ".join([
                    str(instance) for instance in instances]))


class UniqueFieldsMixin(serializers.ModelSerializer):
    """
    Moves `UniqueValidator`'s from the validation stage to the save stage.
    It solves the problem with nested validation for unique fields on update.

    If you want more details, you can read related issues and articles:
    https://github.com/beda-software/drf-writable-nested/issues/1
    http://www.django-rest-framework.org/api-guide/validators/#updating-nested-serializers

    Example of usage:
    ```
        class Child(models.Model):
        field = models.CharField(unique=True)


    class Parent(models.Model):
        child = models.ForeignKey('Child')


    class ChildSerializer(UniqueFieldsMixin, serializers.ModelSerializer):
        class Meta:
            model = Child


    class ParentSerializer(NestedUpdateMixin, serializers.ModelSerializer):
        child = ChildSerializer()

        class Meta:
            model = Parent
    ```

    Note: `UniqueFieldsMixin` must be applied only on the serializer
    which has unique fields.

    Note: When you are using both mixins
    (`UniqueFieldsMixin` and `NestedCreateMixin` or `NestedUpdateMixin`)
    you should put `UniqueFieldsMixin` ahead.
    """
    _unique_fields = []

    def get_fields(self):
        self._unique_fields = []

        fields = super(UniqueFieldsMixin, self).get_fields()
        for field_name, field in fields.items():
            is_unique = any([isinstance(validator, UniqueValidator)
                             for validator in field.validators])
            if is_unique:
                self._unique_fields.append(field_name)
                field.validators = [
                    validator for validator in field.validators
                    if not isinstance(validator, UniqueValidator)]

        return fields

    def _validate_unique_fields(self, validated_data):
        for field_name in self._unique_fields:
            unique_validator = UniqueValidator(self.Meta.model.objects.all())
            unique_validator.set_context(self.fields[field_name])

            try:
                unique_validator(validated_data[field_name])
            except ValidationError as exc:
                raise ValidationError({field_name: exc.detail})

    def create(self, validated_data):
        self._validate_unique_fields(validated_data)
        return super(UniqueFieldsMixin, self).create(validated_data)

    def update(self, instance, validated_data):
        self._validate_unique_fields(validated_data)
        return super(UniqueFieldsMixin, self).update(instance, validated_data)


class RelatedSaveMixin(serializers.Serializer):
    _is_saved = False

    def to_internal_value(self, data):
        self._make_reverse_relations_valid(data)
        return super().to_internal_value(data)

    def _make_reverse_relations_valid(self, data):
        """Make the reverse field optional since we may not have a key for the base object."""
        for field_name, (field, related_field) in self._get_reverse_fields().items():
            if data.get(field.source) is None:
                continue
            if isinstance(field, serializers.ListSerializer):
                field = field.child
            if isinstance(field, serializers.ModelSerializer):
                # find the serializer field matching the reverse model relation
                for sub_field in field.fields.values():
                    if sub_field.source == related_field.name:
                        sub_field.required = False
                        # found the matching field, move on
                        break

    def run_validation(self, data=empty):
        self._validated_data = super().run_validation(data)
        self._errors = {}
        return self._validated_data

    def save(self, **kwargs):
        """We already converted the inputs into a model so we need to save that model"""
        if self._is_saved:
            # prevent recursion when we save a reverse (which tries to save self as a direct)
            return
        # Create or update direct relations (foreign key, one-to-one)
        reverse_relations = self._extract_reverse_relations(kwargs)
        self._save_direct_relations(kwargs)
        instance = super().save(**kwargs)
        self._is_saved = True
        self._save_reverse_relations(reverse_relations, instance=instance)
        return instance

    def _get_reverse_fields(self):
        reverse_fields = OrderedDict()
        if not hasattr(self, 'Meta') or not hasattr(self.Meta, 'model'):
            # No model means no reverse fields (without the need to iterate)
            return reverse_fields
        for field_name, field in self.fields.items():
            if field.read_only:
                continue
            try:
                related_field, direct = self._get_related_field(field)
            except FieldDoesNotExist:
                continue
            if direct:
                continue

            reverse_fields[field_name] = (field, related_field)
        return reverse_fields

    def _get_related_field(self, field):
        model_class = self.Meta.model
        try:
            related_field = model_class._meta.get_field(field.source)
        except FieldDoesNotExist:
            # If `related_name` is not set, field name does not include
            # `_set` -> remove it and check again
            default_postfix = '_set'
            if field.source.endswith(default_postfix):
                related_field = model_class._meta.get_field(
                    field.source[:-len(default_postfix)])
            else:
                raise

        if isinstance(related_field, ForeignObjectRel):
            return related_field.field, False
        return related_field, True

    def _save_direct_relations(self, kwargs):
        """Save direct relations so related objects have FKs when committing the base instance"""
        for field_name, field in self.fields.items():
            if field.read_only:
                continue
            if isinstance(self._validated_data, dict) and self._validated_data.get(field.source) is None:
                continue
            if not isinstance(field, serializers.BaseSerializer):
                continue
            if hasattr(self, 'Meta') and hasattr(self.Meta, 'model'):
                # ModelSerializer (or similar) so we need to exclude reverse relations
                try:
                    _, direct = self._get_related_field(field)
                except FieldDoesNotExist:
                    continue
                if not direct:
                    continue

            # reinject validated_data
            field._validated_data = self._validated_data[field_name]
            self._validated_data[field_name] = field.save(**kwargs.pop(field_name, {}))

    def _extract_reverse_relations(self, kwargs):
        """Removes revere relations from _validated_data to avoid FK integrity issues"""
        # Remove related fields from validated data for future manipulations
        related_objects = []
        for field_name, (field, related_field) in self._get_reverse_fields().items():
            if self._validated_data.get(field.source) is None:
                continue
            serializer = field
            if isinstance(serializer, serializers.ListSerializer):
                serializer = serializer.child
            if isinstance(serializer, serializers.ModelSerializer):
                related_objects.append((
                    field,
                    related_field,
                    self._validated_data.pop(field.source),
                    kwargs.pop(field_name, {}),
                ))
        return related_objects

    def _save_reverse_relations(self, related_objects, instance):
        """Inject the current object as the FK in the reverse related objects and save them"""
        for field, related_field, data, kwargs in related_objects:
            # inject the PK from the instance
            if isinstance(field, serializers.ListSerializer):
                for obj in data:
                    obj[related_field.name] = instance
            elif isinstance(field, serializers.ModelSerializer):
                data[related_field.name] = instance
            else:
                raise Exception("unexpected serializer type")

            # reinject validated_data
            field._validated_data = data
            field.save(**kwargs)


class GetOrCreateListSerializer(serializers.ListSerializer):
    """Need a special save() method that cascades to the list of child instances"""
    def save(self, **kwargs):
        """
        Save and return a list of object instances.
        """
        # Guard against incorrect use of `serializer.save(commit=False)`
        assert 'commit' not in kwargs, (
            "'commit' is not a valid keyword argument to the 'save()' method. "
            "If you need to access data before committing to the database then "
            "inspect 'serializer.validated_data' instead. "
            "You can also pass additional keyword arguments to 'save()' if you "
            "need to set extra attributes on the saved model instance. "
            "For example: 'serializer.save(owner=request.user)'.'"
        )

        new_values = []

        for item in self._validated_data:
            # integrate save kwargs
            self.child._validated_data = item
            # since we reuse the serializer, we need to re-inject the new _validated_data using save kwargs
            new_values.append(self.child.save(**kwargs))

        return new_values

    def run_validation(self, data=empty):
        """Since a nested serializer is treated like a Field, `is_valid` will not be called so we need to set
        _validated_data in the mixin."""
        self._validated_data = super().run_validation(data)
        return self._validated_data


class GetOrCreateNestedSerializerMixin(RelatedSaveMixin):
    """Transcodes a raw data stream into a Model instance, using get-or-create logic."""
    default_list_serializer = GetOrCreateListSerializer
    DEFAULT_MATCH_ON = ['pk']
    queryset = None

    @classmethod
    def many_init(cls, *args, **kwargs):
        # inject the default into list_serializer_class (if not present)
        meta = getattr(cls, 'Meta', None)
        if meta is None:
            class Meta:
                pass
            meta = Meta
            setattr(cls, 'Meta', meta)
        list_serializer_class = getattr(meta, 'list_serializer_class', None)
        if list_serializer_class is None:
            setattr(meta, 'list_serializer_class', cls.default_list_serializer)
        assert issubclass(meta.list_serializer_class, GetOrCreateListSerializer), \
            "ChildNestedSerializerMixin expects a GetOrCreateListSerializer for correct save behavior.  Please override " \
            "Meta.list_serializer_class and provide an appropriate class."
        return super(GetOrCreateNestedSerializerMixin, cls).many_init(*args, **kwargs)

    def __init__(self, *args, **kwargs):
        self.queryset = kwargs.pop('queryset', self.queryset)
        if self.queryset is None and hasattr(self, 'Meta') and hasattr(self.Meta, 'model'):
            self.queryset = self.Meta.model.objects.all()
        assert self.queryset is not None, \
            "GetOrCreateMixin requires a `queryset` on the Field or a `queryset` kwarg"
        self.match_on = kwargs.pop('match_on', self.DEFAULT_MATCH_ON)
        assert self.match_on == '__all__' or isinstance(self.match_on, (tuple, list, set)), \
            "match_on only accepts as Collection of strings or the special value __all__"
        if isinstance(self.match_on, (tuple, list, set)):
            for match in self.match_on:
                assert isinstance(match, str), "match_on collection can only contain strings"
        super(GetOrCreateNestedSerializerMixin, self).__init__(*args, **kwargs)

    def run_validation(self, data=empty):
        """A nested serializer is treated like a Field so `is_valid` will not be called and `_validated_data` not set."""
        # ensure Unique and UniqueTogether don't collide with a DB match
        validators = self.remove_validation_unique()
        validated_data = super().run_validation(data)
        # restore Unique or UniqueTogether
        self.restore_validation_unique(validators)
        return self.validated_data

    def remove_validation_unique(self):
        """
        Removes unique validators from a serializers.  This is critical for get-or-create style serialization.  It can also
        be used to distinguish 409 errors from client-side validation errors.
        """
        fields = {}
        # extract unique validators
        for name, field in self.fields.items():
            fields[name] = []
            assert hasattr(field, 'validators'), "no validators on {}".format(field.__class__.__name__)
            for validator in field.validators:
                if isinstance(validator, UniqueValidator):
                    fields[name].append(validator)
            for validator in fields[name]:
                field.validators.remove(validator)
        # extract unique_together validators
        fields['_'] = []
        for validator in self.validators:
            if isinstance(validator, UniqueTogetherValidator):
                fields['_'].append(validator)
        for validator in fields['_']:
            self.validators.remove(validator)
        return fields

    def restore_validation_unique(self, unique_validators):
        together_validators = unique_validators.pop('_')
        for serializer in together_validators:
            self.validators.append(serializer)
        fields = self.fields.items()
        for name, validators in unique_validators.items():
            for validator in validators:
                fields['name'].validators.append(validator)

    def save(self, **kwargs):
        """We already converted the inputs into a model so we need to save that model"""
        for k, v in kwargs.items():
            self._validated_data[k] = v

        # Create or update direct relations (foreign key, one-to-one)
        related_objects = self._extract_reverse_relations(kwargs)
        self._save_direct_relations(kwargs)

        # TODO: move to a specialized class (easier to subclass)
        try:
            match_on = {}
            for field_name, field in self.get_fields().items():
                if self.match_on == '__all__' or field_name in self.match_on:
                    match_on[field.source or field_name] = self._validated_data.get(field_name)
            # a parent serializer may inject a value that isn't among the fields, but is in `match_on`
            for key in self.match_on:
                if key not in self.get_fields().keys():
                    match_on[key] = self._validated_data.get(key)
            match = self.queryset.get(**match_on)
            for k, v in self._validated_data.items():
                setattr(match, k, v)
        except ObjectDoesNotExist:
            match = self.queryset.model(**self._validated_data)
        except (TypeError, ValueError):
            self.fail('incorrect_type', data_type=type(self._validated_data).__name__)
        match.save()

        self._save_reverse_relations(related_objects, instance=match)
        return match
