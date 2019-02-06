# Copyright (c) 2018 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from unittest.mock import Mock

import pytest

from owca.allocations import AllocationsDict, BoxedNumeric, AllocationValue, InvalidAllocations
from owca.testing import allocation_metric


@pytest.mark.parametrize(
    'current, new, expected_target, expected_changeset', [
        ({}, {},
         {}, None),
        ({'x': 2}, {},
         {'x': 2}, None),
        ({'a': 0.2}, {},
         {'a': 0.2}, None),
        ({'a': 0.2}, {'a': 0.2},
         {'a': 0.2}, None),
        ({'b': 2}, {'b': 3},
         {'b': 3}, {'b': 3}),
        ({'a': 0.2, 'b': 0.4}, {'a': 0.2, 'b': 0.5},
         {'a': 0.2, 'b': 0.5}, {'b': 0.5}),
        ({}, {'a': 0.2, 'b': 0.5},
         {'a': 0.2, 'b': 0.5}, {'a': 0.2, 'b': 0.5}),
        # Recursively one more level (we use dict to show it)
        (dict(t1={'a': 2}), {},
         dict(t1={'a': 2}), None),
        (dict(t1={'a': 2}), dict(t1={'a': 2.01}),  # small enough to ignore
         dict(t1={'a': 2}), None),
        (dict(t1={'a': 2}), dict(t1={'a': 2.1}),  # big enough to notice
         dict(t1={'a': 2.1}), dict(t1={'a': 2.1})),
        (dict(t1={'a': 2}), dict(t1={'a': 2}),
         dict(t1={'a': 2}), None),
        (dict(t1={'a': 1}), dict(t1={'b': 2}, t2={'b': 3}),
         dict(t1={'a': 1, 'b': 2}, t2={'b': 3}), dict(t1={'b': 2}, t2={'b': 3})),
    ]
)
def test_allocations_dict_merging(current, new,
                                  expected_target, expected_changeset):
    def convert_to_allocations_dict(d: dict):
        if d is not None:
            registry = {
                float: BoxedNumeric,
                int: BoxedNumeric,
                dict: convert_to_allocations_dict,
            }
            return AllocationsDict({k: registry[type(v)](v) for k, v in d.items()})
        else:
            return None

    # Conversion
    current_dict = convert_to_allocations_dict(current)
    new_dict = convert_to_allocations_dict(new)

    # Merge
    got_target_dict, got_changeset_dict = new_dict.calculate_changeset(current_dict)

    assert got_target_dict == convert_to_allocations_dict(expected_target)
    assert got_changeset_dict == convert_to_allocations_dict(expected_changeset)


@pytest.mark.parametrize('allocation_dict, expected_error', [
    (AllocationsDict({'bad_generic': Mock(spec=AllocationValue, validate=Mock(
        side_effect=InvalidAllocations('some generic error')))}),
     'some generic error'),
    (AllocationsDict({'x': BoxedNumeric(-1)}), 'does not belong to range'),
    (AllocationsDict({'x': AllocationsDict({'y': BoxedNumeric(-1)})}), 'does not belong to range'),
])
def test_allocation_value_validate(allocation_dict, expected_error):
    with pytest.raises(InvalidAllocations, match=expected_error):
        allocation_dict.validate()


@pytest.mark.parametrize('allocation_value, expected_metrics', [
    (AllocationsDict({}),
     []),
    (BoxedNumeric(2),
     [allocation_metric(None, 2)]),
    (AllocationsDict({'x': BoxedNumeric(2), 'y': BoxedNumeric(3)}),
     [allocation_metric(None, 2), allocation_metric(None, 3)]),
    (AllocationsDict({'x': BoxedNumeric(2), 'y': BoxedNumeric(3)}),
     [allocation_metric(None, 2), allocation_metric(None, 3)]),
    (AllocationsDict({'x': BoxedNumeric(2),
                      'y': BoxedNumeric(3.5, common_labels=dict(foo='bar'))}),
     [allocation_metric(None, 2), allocation_metric(None, 3.5, foo='bar')]),
])
def test_allocation_values_metrics(allocation_value: AllocationValue, expected_metrics):
    got_metrics = allocation_value.generate_metrics()
    assert got_metrics == expected_metrics
