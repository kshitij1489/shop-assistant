"""Versioned full-menu format accepted by the generic external JSON adapter."""
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator
from .policy import Strict

Identity = Annotated[str, Field(min_length=1, max_length=200, pattern=r'.*\S.*')]
Name = Annotated[str, Field(min_length=1, max_length=255, pattern=r'.*\S.*')]
Price = Annotated[Decimal, Field(ge=0, max_digits=10, decimal_places=2, allow_inf_nan=False)]
Count = Annotated[int, Field(ge=0, le=10000, strict=True)]


def unique(rows, key='external_id'):
    values = [getattr(row, key) for row in rows]
    if len(set(values)) != len(values):
        raise ValueError(f'Duplicate {key} in menu snapshot.')


class Category(Strict):
    external_id: Identity
    name: Name
    sort_order: Count = 0
    available: bool = Field(strict=True)


class Variant(Strict):
    external_id: Identity
    name: str = Field(min_length=1, max_length=50, pattern=r'.*\S.*')
    price: Price
    available: bool = Field(strict=True)
    sort_order: Count = 0


class Modifier(Strict):
    external_id: Identity
    name: Name
    price: Price
    available: bool = Field(strict=True)
    min_quantity: int = Field(default=1, ge=1, le=10000, strict=True)
    max_quantity: int = Field(default=1, ge=1, le=10000, strict=True)

    @model_validator(mode='after')
    def quantities(self):
        if self.min_quantity > self.max_quantity:
            raise ValueError('Modifier minimum exceeds maximum quantity.')
        return self


class ModifierGroup(Strict):
    external_id: Identity
    name: Name
    options: list[Modifier] = Field(max_length=500)

    @model_validator(mode='after')
    def identities(self):
        unique(self.options)
        return self


class ModifierRule(Strict):
    group_id: Identity
    min_selections: Count
    max_selections: Count
    variant_ids: list[Identity] = Field(default_factory=list, max_length=500)


class Item(Strict):
    external_id: Identity
    name: Name
    description: str = Field(default='', max_length=10000)
    available: bool = Field(strict=True)
    category_id: Identity | None = None
    variants: list[Variant] = Field(min_length=1, max_length=500)
    modifier_groups: list[ModifierRule] = Field(max_length=100)

    @model_validator(mode='after')
    def identities(self):
        unique(self.variants)
        unique(self.modifier_groups, 'group_id')
        labels = [v.name.strip().lower() for v in self.variants if v.available]
        if len(set(labels)) != len(labels):
            raise ValueError('Available variant labels must be unique within an item.')
        return self


class MenuSnapshot(Strict):
    schema_version: Literal[1]
    complete: Literal[True]
    source_generation: UUID
    sequence: int = Field(ge=1, le=9223372036854775807, strict=True)
    revision: str = Field(min_length=1, max_length=100)
    observed_at: datetime
    currency: str = Field(pattern=r'^[A-Z]{3}$')
    categories: list[Category] = Field(max_length=1000)
    modifier_groups: list[ModifierGroup] = Field(max_length=1000)
    items: list[Item] = Field(max_length=5000)

    @model_validator(mode='before')
    @classmethod
    def complete_export(cls, data):
        if not isinstance(data, dict) or data.get('complete') is not True:
            raise ValueError('A snapshot must explicitly declare complete=true.')
        return data

    @model_validator(mode='after')
    def references(self):
        if self.observed_at.tzinfo is None:
            raise ValueError('observed_at requires an explicit timezone.')
        for rows in (self.categories, self.modifier_groups, self.items):
            unique(rows)
        item_names = [i.name.strip().lower() for i in self.items]
        if len(set(item_names)) != len(item_names):
            raise ValueError('Item names must be unique within the tenant catalog.')
        names = [c.name.strip().lower() for c in self.categories]
        if len(set(names)) != len(names):
            raise ValueError('Category names must be unique.')
        categories = {c.external_id for c in self.categories}
        groups = {g.external_id: g for g in self.modifier_groups}
        for item in self.items:
            if item.category_id is not None and item.category_id not in categories:
                raise ValueError('Item refers to an unknown category.')
            variants = {v.external_id for v in item.variants}
            for rule in item.modifier_groups:
                group = groups.get(rule.group_id)
                if group is None or not set(rule.variant_ids) <= variants:
                    raise ValueError('Modifier rule refers to an unknown group or variant.')
                if not rule.min_selections <= rule.max_selections <= len(group.options):
                    raise ValueError('Invalid modifier selection limits.')
        return self
