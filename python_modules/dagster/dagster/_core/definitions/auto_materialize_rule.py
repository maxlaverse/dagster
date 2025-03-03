import datetime
import functools
from abc import ABC, abstractmethod, abstractproperty
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from typing import (
    TYPE_CHECKING,
    AbstractSet,
    Callable,
    Dict,
    FrozenSet,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
    Set,
    Tuple,
    cast,
)

import dagster._check as check
from dagster._annotations import public
from dagster._core.definitions.data_time import CachingDataTimeResolver
from dagster._core.definitions.events import AssetKey, AssetKeyPartitionKey
from dagster._core.definitions.freshness_based_auto_materialize import (
    freshness_evaluation_results_for_asset_key,
)
from dagster._core.definitions.partition_mapping import IdentityPartitionMapping
from dagster._core.definitions.time_window_partition_mapping import TimeWindowPartitionMapping
from dagster._serdes.serdes import (
    NamedTupleSerializer,
    UnpackContext,
    UnpackedValue,
    WhitelistMap,
    whitelist_for_serdes,
)
from dagster._utils.caching_instance_queryer import CachingInstanceQueryer

from .asset_graph import AssetGraph, sort_key_for_asset_partition
from .partition import SerializedPartitionsSubset

if TYPE_CHECKING:
    from dagster._core.definitions.asset_daemon_context import AssetDaemonContext
    from dagster._core.definitions.asset_daemon_cursor import AssetDaemonCursor
    from dagster._core.instance import DynamicPartitionsStore


@whitelist_for_serdes
class AutoMaterializeDecisionType(Enum):
    """Represents the set of results of the auto-materialize logic.

    MATERIALIZE: The asset should be materialized by a run kicked off on this tick
    SKIP: The asset should not be materialized by a run kicked off on this tick, because future
        ticks are expected to materialize it.
    DISCARD: The asset should not be materialized by a run kicked off on this tick, but future
        ticks are not expected to materialize it.
    """

    MATERIALIZE = "MATERIALIZE"
    SKIP = "SKIP"
    DISCARD = "DISCARD"


class AutoMaterializeRuleEvaluationData(ABC):
    pass


@whitelist_for_serdes
class TextRuleEvaluationData(
    AutoMaterializeRuleEvaluationData,
    NamedTuple("_TextRuleEvaluationData", [("text", str)]),
):
    pass


@whitelist_for_serdes
class ParentUpdatedRuleEvaluationData(
    AutoMaterializeRuleEvaluationData,
    NamedTuple(
        "_ParentUpdatedRuleEvaluationData",
        [
            ("updated_asset_keys", FrozenSet[AssetKey]),
            ("will_update_asset_keys", FrozenSet[AssetKey]),
        ],
    ),
):
    pass


@whitelist_for_serdes
class WaitingOnAssetsRuleEvaluationData(
    AutoMaterializeRuleEvaluationData,
    NamedTuple(
        "_WaitingOnParentRuleEvaluationData",
        [("waiting_on_asset_keys", FrozenSet[AssetKey])],
    ),
):
    pass


@whitelist_for_serdes
class AutoMaterializeRuleSnapshot(NamedTuple):
    """A serializable snapshot of an AutoMaterializeRule for historical evaluations."""

    class_name: str
    description: str
    decision_type: AutoMaterializeDecisionType

    @staticmethod
    def from_rule(rule: "AutoMaterializeRule") -> "AutoMaterializeRuleSnapshot":
        return AutoMaterializeRuleSnapshot(
            class_name=rule.__class__.__name__,
            description=rule.description,
            decision_type=rule.decision_type,
        )


@whitelist_for_serdes
class AutoMaterializeRuleEvaluation(NamedTuple):
    rule_snapshot: AutoMaterializeRuleSnapshot
    evaluation_data: Optional[AutoMaterializeRuleEvaluationData]


@dataclass(frozen=True)
class RuleEvaluationContext:
    asset_key: AssetKey
    cursor: "AssetDaemonCursor"
    instance_queryer: CachingInstanceQueryer
    data_time_resolver: CachingDataTimeResolver
    will_materialize_mapping: Mapping[AssetKey, AbstractSet[AssetKeyPartitionKey]]
    expected_data_time_mapping: Mapping[AssetKey, Optional[datetime.datetime]]
    candidates: AbstractSet[AssetKeyPartitionKey]
    daemon_context: "AssetDaemonContext"

    @property
    def asset_graph(self) -> AssetGraph:
        return self.instance_queryer.asset_graph

    @property
    def previous_tick_evaluation(self) -> Optional["AutoMaterializeAssetEvaluation"]:
        """Returns the evaluation of the asset on the previous tick."""
        return self.cursor.latest_evaluation_by_asset_key.get(self.asset_key)

    @functools.cached_property
    def previous_tick_requested_or_discarded_asset_partitions(
        self,
    ) -> AbstractSet[AssetKeyPartitionKey]:
        """Returns the set of asset partitions that were requested or discarded on the previous tick."""
        if not self.previous_tick_evaluation:
            return set()
        return self.previous_tick_evaluation.get_requested_or_discarded_asset_partitions(
            asset_graph=self.asset_graph
        )

    @functools.cached_property
    def previous_tick_evaluated_asset_partitions(
        self,
    ) -> AbstractSet[AssetKeyPartitionKey]:
        """Returns the set of asset partitions that were evaluated on the previous tick."""
        if not self.previous_tick_evaluation:
            return set()
        return self.previous_tick_evaluation.get_evaluated_asset_partitions(
            asset_graph=self.asset_graph
        )

    def get_previous_tick_results(self, rule: "AutoMaterializeRule") -> "RuleEvaluationResults":
        """Returns the results that were calculated for a given rule on the previous tick."""
        if not self.previous_tick_evaluation:
            return []
        return self.previous_tick_evaluation.get_rule_evaluation_results(
            rule_snapshot=rule.to_snapshot(), asset_graph=self.asset_graph
        )

    def get_candidates_not_evaluated_by_rule_on_previous_tick(
        self,
    ) -> AbstractSet[AssetKeyPartitionKey]:
        """Returns the set of candidates that were not evaluated by the rule that is currently being
        evaluated on the previous tick.

        Any asset partition that was evaluated by any rule on the previous tick must have been
        evaluated by *all* skip rules.
        """
        return self.candidates - self.previous_tick_evaluated_asset_partitions

    def get_candidates_with_updated_or_will_update_parents(
        self,
    ) -> AbstractSet[AssetKeyPartitionKey]:
        """Returns the set of candidate asset partitions whose parents have been updated since the
        last tick or will be requested on this tick.

        Many rules depend on the state of the asset's parents, so this function is useful for
        finding asset partitions that should be re-evaluated.
        """
        updated_parents = self.get_asset_partitions_with_updated_parents()
        will_update_parents = set(self.get_will_update_parent_mapping().keys())
        return self.candidates & (updated_parents | will_update_parents)

    def materialized_requested_or_discarded_since_previous_tick(
        self, asset_partition: AssetKeyPartitionKey
    ) -> bool:
        """Returns whether an asset partition has been materialized, requested, or discarded since
        the last tick.
        """
        if asset_partition in self.previous_tick_requested_or_discarded_asset_partitions:
            return True
        return self.instance_queryer.asset_partition_has_materialization_or_observation(
            asset_partition, after_cursor=self.cursor.latest_storage_id
        )

    def materializable_in_same_run(self, child_key: AssetKey, parent_key: AssetKey) -> bool:
        """Returns whether a child asset can be materialized in the same run as a parent asset."""
        from dagster._core.definitions.external_asset_graph import ExternalAssetGraph

        return (
            # both assets must be materializable
            child_key in self.asset_graph.materializable_asset_keys
            and parent_key in self.asset_graph.materializable_asset_keys
            # the parent must have the same partitioning
            and self.asset_graph.have_same_partitioning(child_key, parent_key)
            # the parent must have a simple partition mapping to the child
            and (
                not self.asset_graph.is_partitioned(parent_key)
                or isinstance(
                    self.asset_graph.get_partition_mapping(child_key, parent_key),
                    (TimeWindowPartitionMapping, IdentityPartitionMapping),
                )
            )
            # the parent must be in the same repository to be materialized alongside the candidate
            and (
                not isinstance(self.asset_graph, ExternalAssetGraph)
                or self.asset_graph.get_repository_handle(child_key)
                == self.asset_graph.get_repository_handle(parent_key)
            )
        )

    def get_parents_that_will_not_be_materialized_on_current_tick(
        self, *, asset_partition: AssetKeyPartitionKey
    ) -> AbstractSet[AssetKeyPartitionKey]:
        """Returns the set of parent asset partitions that will not be updated in the same run of
        this asset partition if a run is launched for this asset partition on this tick.
        """
        return {
            parent
            for parent in self.asset_graph.get_parents_partitions(
                dynamic_partitions_store=self.instance_queryer,
                current_time=self.instance_queryer.evaluation_time,
                asset_key=asset_partition.asset_key,
                partition_key=asset_partition.partition_key,
            ).parent_partitions
            if parent not in self.will_materialize_mapping.get(parent.asset_key, set())
            or not self.materializable_in_same_run(asset_partition.asset_key, parent.asset_key)
        }

    def get_asset_partitions_with_updated_parents(self) -> AbstractSet[AssetKeyPartitionKey]:
        """Returns the set of asset partitions for the current key which have parents that updated
        since the last tick.
        """
        return self.daemon_context.get_asset_partitions_with_newly_updated_parents_for_key(
            self.asset_key
        )

    def get_will_update_parent_mapping(
        self,
    ) -> Mapping[AssetKeyPartitionKey, AbstractSet[AssetKey]]:
        """Returns a mapping from asset partitions of the current asset to the set of parent keys
        which will be requested this tick and can execute in the same run as the current asset.
        """
        will_update_parents_by_asset_partition = defaultdict(set)
        # these are the set of parents that will be requested this tick and can be materialized in
        # the same run as this asset
        for parent_key in self.asset_graph.get_parents(self.asset_key):
            if not self.materializable_in_same_run(self.asset_key, parent_key):
                continue
            for parent_partition in self.will_materialize_mapping.get(parent_key, set()):
                asset_partition = AssetKeyPartitionKey(
                    self.asset_key, parent_partition.partition_key
                )
                will_update_parents_by_asset_partition[asset_partition].add(parent_key)

        return will_update_parents_by_asset_partition

    def get_asset_partitions_by_asset_key(
        self,
        asset_partitions: AbstractSet[AssetKeyPartitionKey],
    ) -> Mapping[AssetKey, Set[AssetKeyPartitionKey]]:
        asset_partitions_by_asset_key: Dict[AssetKey, Set[AssetKeyPartitionKey]] = defaultdict(set)
        for parent in asset_partitions:
            asset_partitions_by_asset_key[parent.asset_key].add(parent)

        return asset_partitions_by_asset_key


RuleEvaluationResults = Sequence[Tuple[Optional[AutoMaterializeRuleEvaluationData], AbstractSet]]


class AutoMaterializeRule(ABC):
    """An AutoMaterializeRule defines a bit of logic which helps determine if a materialization
    should be kicked off for a given asset partition.

    Each rule can have one of two decision types, `MATERIALIZE` (indicating that an asset partition
    should be materialized) or `SKIP` (indicating that the asset partition should not be
    materialized).

    Materialize rules are evaluated first, and skip rules operate over the set of candidates that
    are produced by the materialize rules. Other than that, there is no ordering between rules.
    """

    @abstractproperty
    def decision_type(self) -> AutoMaterializeDecisionType:
        """The decision type of the rule (either `MATERIALIZE` or `SKIP`)."""
        ...

    @abstractproperty
    def description(self) -> str:
        """A human-readable description of this rule. As a basic guideline, this string should
        complete the sentence: 'Indicates an asset should be (materialize/skipped) when ____'.
        """
        ...

    def add_evaluation_data_from_previous_tick(
        self,
        context: RuleEvaluationContext,
        asset_partitions_by_evaluation_data: Mapping[
            Optional[AutoMaterializeRuleEvaluationData], Set[AssetKeyPartitionKey]
        ],
        should_use_past_data_fn: Callable[[AssetKeyPartitionKey], bool],
    ) -> "RuleEvaluationResults":
        """Combines a given set of evaluation data with evaluation data from the previous tick. The
        returned value will include the union of the evaluation data contained within
        `asset_partitions_by_evaluation_data` and the evaluation data calculated for asset
        partitions on the previous tick for which `should_use_past_data_fn` evaluates to `True`.

        Args:
            context: The current RuleEvaluationContext.
            asset_partitions_by_evaluation_data: A mapping from evaluation data to the set of asset
                partitions that the rule applies to.
            should_use_past_data_fn: A function that returns whether a given asset partition from the
                previous tick should be included in the results of this tick.
        """
        asset_partitions_by_evaluation_data = defaultdict(set, asset_partitions_by_evaluation_data)
        evaluated_asset_partitions = set().union(*asset_partitions_by_evaluation_data.values())
        for evaluation_data, asset_partitions in context.get_previous_tick_results(self):
            for ap in asset_partitions:
                # evaluated data from this tick takes precedence over data from the previous tick
                if ap in evaluated_asset_partitions:
                    continue
                elif should_use_past_data_fn(ap):
                    asset_partitions_by_evaluation_data[evaluation_data].add(ap)

        return list(asset_partitions_by_evaluation_data.items())

    @abstractmethod
    def evaluate_for_asset(self, context: RuleEvaluationContext) -> RuleEvaluationResults:
        """The core evaluation function for the rule. This function takes in a context object and
        returns a mapping from evaluated rules to the set of asset partitions that the rule applies
        to.
        """
        ...

    @public
    @staticmethod
    def materialize_on_required_for_freshness() -> "MaterializeOnRequiredForFreshnessRule":
        """Materialize an asset partition if it is required to satisfy a freshness policy of this
        asset or one of its downstream assets.

        Note: This rule has no effect on partitioned assets.
        """
        return MaterializeOnRequiredForFreshnessRule()

    @public
    @staticmethod
    def materialize_on_parent_updated() -> "MaterializeOnParentUpdatedRule":
        """Materialize an asset partition if one of its parents has been updated more recently
        than it has.

        Note: For time-partitioned or dynamic-partitioned assets downstream of an unpartitioned
        asset, this rule will only fire for the most recent partition of the downstream.
        """
        return MaterializeOnParentUpdatedRule()

    @public
    @staticmethod
    def materialize_on_missing() -> "MaterializeOnMissingRule":
        """Materialize an asset partition if it has never been materialized before. This rule will
        not fire for non-root assets unless that asset's parents have been updated.
        """
        return MaterializeOnMissingRule()

    @public
    @staticmethod
    def skip_on_parent_missing() -> "SkipOnParentMissingRule":
        """Skip materializing an asset partition if one of its parent asset partitions has never
        been materialized (for regular assets) or observed (for observable source assets).
        """
        return SkipOnParentMissingRule()

    @public
    @staticmethod
    def skip_on_parent_outdated() -> "SkipOnParentOutdatedRule":
        """Skip materializing an asset partition if any of its parents has not incorporated the
        latest data from its ancestors.
        """
        return SkipOnParentOutdatedRule()

    @public
    @staticmethod
    def skip_on_not_all_parents_updated(
        require_update_for_all_parent_partitions: bool = False,
    ) -> "SkipOnNotAllParentsUpdatedRule":
        """Skip materializing an asset partition if any of its parents have not been updated since
        the asset's last materialization.

        Attributes:
            require_update_for_all_parent_partitions (Optional[bool]): Applies only to an unpartitioned
                asset or an asset partition that depends on more than one partition in any upstream asset.
                If true, requires all upstream partitions in each upstream asset to be materialized since
                the downstream asset's last materialization in order to update it. If false, requires at
                least one upstream partition in each upstream asset to be materialized since the downstream
                asset's last materialization in order to update it. Defaults to false.
        """
        return SkipOnNotAllParentsUpdatedRule(require_update_for_all_parent_partitions)

    @public
    @staticmethod
    def skip_on_required_but_nonexistent_parents() -> "SkipOnRequiredButNonexistentParentsRule":
        """Skip an asset partition if it depends on parent partitions that do not exist.

        For example, imagine a downstream asset is time-partitioned, starting in 2022, but has a
        time-partitioned parent which starts in 2023. This rule will skip attempting to materialize
        downstream partitions from before 2023, since the parent partitions do not exist.
        """
        return SkipOnRequiredButNonexistentParentsRule()

    @public
    @staticmethod
    def skip_on_backfill_in_progress(
        all_partitions: bool = False,
    ) -> "SkipOnBackfillInProgressRule":
        """Skip an asset's partitions if targeted by an in-progress backfill.

        Attributes:
            all_partitions (bool): If True, skips all partitions of the asset being backfilled,
                regardless of whether the specific partition is targeted by a backfill.
                If False, skips only partitions targeted by a backfill. Defaults to False.
        """
        return SkipOnBackfillInProgressRule(all_partitions)

    def to_snapshot(self) -> AutoMaterializeRuleSnapshot:
        """Returns a serializable snapshot of this rule for historical evaluations."""
        return AutoMaterializeRuleSnapshot.from_rule(self)

    def __eq__(self, other) -> bool:
        # override the default NamedTuple __eq__ method to factor in types
        return type(self) == type(other) and super().__eq__(other)

    def __hash__(self) -> int:
        # override the default NamedTuple __hash__ method to factor in types
        return hash(hash(type(self)) + super().__hash__())


@whitelist_for_serdes
class MaterializeOnRequiredForFreshnessRule(
    AutoMaterializeRule, NamedTuple("_MaterializeOnRequiredForFreshnessRule", [])
):
    @property
    def decision_type(self) -> AutoMaterializeDecisionType:
        return AutoMaterializeDecisionType.MATERIALIZE

    @property
    def description(self) -> str:
        return "required to meet this or downstream asset's freshness policy"

    def evaluate_for_asset(self, context: RuleEvaluationContext) -> RuleEvaluationResults:
        freshness_conditions = freshness_evaluation_results_for_asset_key(
            asset_key=context.asset_key,
            data_time_resolver=context.data_time_resolver,
            asset_graph=context.asset_graph,
            current_time=context.instance_queryer.evaluation_time,
            will_materialize_mapping=context.will_materialize_mapping,
            expected_data_time_mapping=context.expected_data_time_mapping,
        )
        return freshness_conditions


@whitelist_for_serdes
class MaterializeOnParentUpdatedRule(
    AutoMaterializeRule, NamedTuple("_MaterializeOnParentUpdatedRule", [])
):
    @property
    def decision_type(self) -> AutoMaterializeDecisionType:
        return AutoMaterializeDecisionType.MATERIALIZE

    @property
    def description(self) -> str:
        return "upstream data has changed since latest materialization"

    def evaluate_for_asset(self, context: RuleEvaluationContext) -> RuleEvaluationResults:
        """Evaluates the set of asset partitions of this asset whose parents have been updated,
        or will update on this tick.
        """
        asset_partitions_by_evaluation_data = defaultdict(set)

        will_update_parents_by_asset_partition = context.get_will_update_parent_mapping()

        # the set of asset partitions whose parents have been updated since last tick, or will be
        # requested this tick.
        has_or_will_update = context.get_asset_partitions_with_updated_parents() | set(
            will_update_parents_by_asset_partition.keys()
        )
        for asset_partition in has_or_will_update:
            parent_asset_partitions = context.asset_graph.get_parents_partitions(
                dynamic_partitions_store=context.instance_queryer,
                current_time=context.instance_queryer.evaluation_time,
                asset_key=asset_partition.asset_key,
                partition_key=asset_partition.partition_key,
            ).parent_partitions

            updated_parent_asset_partitions = context.instance_queryer.get_parent_asset_partitions_updated_after_child(
                asset_partition,
                parent_asset_partitions,
                # do a precise check for updated parents, factoring in data versions, as long as
                # we're within reasonable limits on the number of partitions to check
                respect_materialization_data_versions=context.daemon_context.respect_materialization_data_versions
                and len(parent_asset_partitions | has_or_will_update) < 100,
                # ignore self-dependencies when checking for updated parents, to avoid historical
                # rematerializations from causing a chain of materializations to be kicked off
                ignored_parent_keys={context.asset_key},
            )
            updated_parents = {parent.asset_key for parent in updated_parent_asset_partitions}
            will_update_parents = will_update_parents_by_asset_partition[asset_partition]

            if updated_parents or will_update_parents:
                asset_partitions_by_evaluation_data[
                    ParentUpdatedRuleEvaluationData(
                        updated_asset_keys=frozenset(updated_parents),
                        will_update_asset_keys=frozenset(will_update_parents),
                    )
                ].add(asset_partition)

        return self.add_evaluation_data_from_previous_tick(
            context,
            asset_partitions_by_evaluation_data,
            should_use_past_data_fn=lambda ap: not context.materialized_requested_or_discarded_since_previous_tick(
                ap
            ),
        )


@whitelist_for_serdes
class MaterializeOnMissingRule(AutoMaterializeRule, NamedTuple("_MaterializeOnMissingRule", [])):
    @property
    def decision_type(self) -> AutoMaterializeDecisionType:
        return AutoMaterializeDecisionType.MATERIALIZE

    @property
    def description(self) -> str:
        return "materialization is missing"

    def evaluate_for_asset(self, context: RuleEvaluationContext) -> RuleEvaluationResults:
        """Evaluates the set of asset partitions for this asset which are missing and were not
        previously discarded. Currently only applies to root asset partitions and asset partitions
        with updated parents.
        """
        asset_partitions_by_evaluation_data = defaultdict(set)

        missing_asset_partitions = set(
            context.daemon_context.get_never_handled_root_asset_partitions_for_key(
                context.asset_key
            )
        )
        # in addition to missing root asset partitions, check any asset partitions with updated
        # parents to see if they're missing
        for (
            candidate
        ) in context.daemon_context.get_asset_partitions_with_newly_updated_parents_for_key(
            context.asset_key
        ):
            if not context.instance_queryer.asset_partition_has_materialization_or_observation(
                candidate
            ):
                missing_asset_partitions |= {candidate}

        if missing_asset_partitions:
            asset_partitions_by_evaluation_data[None] = missing_asset_partitions

        return self.add_evaluation_data_from_previous_tick(
            context,
            asset_partitions_by_evaluation_data,
            should_use_past_data_fn=lambda ap: ap not in missing_asset_partitions
            and not context.materialized_requested_or_discarded_since_previous_tick(ap),
        )


@whitelist_for_serdes
class SkipOnParentOutdatedRule(AutoMaterializeRule, NamedTuple("_SkipOnParentOutdatedRule", [])):
    @property
    def decision_type(self) -> AutoMaterializeDecisionType:
        return AutoMaterializeDecisionType.SKIP

    @property
    def description(self) -> str:
        return "waiting on upstream data to be up to date"

    def evaluate_for_asset(self, context: RuleEvaluationContext) -> RuleEvaluationResults:
        asset_partitions_by_evaluation_data = defaultdict(set)

        # only need to evaluate net-new candidates and candidates whose parents have changed
        candidates_to_evaluate = (
            context.get_candidates_not_evaluated_by_rule_on_previous_tick()
            | context.get_candidates_with_updated_or_will_update_parents()
        )
        for candidate in candidates_to_evaluate:
            outdated_ancestors = set()
            # find the root cause of why this asset partition's parents are outdated (if any)
            for parent in context.get_parents_that_will_not_be_materialized_on_current_tick(
                asset_partition=candidate
            ):
                if context.instance_queryer.have_ignorable_partition_mapping_for_outdated(
                    candidate.asset_key, parent.asset_key
                ):
                    continue
                outdated_ancestors.update(
                    context.instance_queryer.get_outdated_ancestors(asset_partition=parent)
                )
            if outdated_ancestors:
                asset_partitions_by_evaluation_data[
                    WaitingOnAssetsRuleEvaluationData(frozenset(outdated_ancestors))
                ].add(candidate)

        return self.add_evaluation_data_from_previous_tick(
            context,
            asset_partitions_by_evaluation_data,
            should_use_past_data_fn=lambda ap: ap not in candidates_to_evaluate,
        )


@whitelist_for_serdes
class SkipOnParentMissingRule(AutoMaterializeRule, NamedTuple("_SkipOnParentMissingRule", [])):
    @property
    def decision_type(self) -> AutoMaterializeDecisionType:
        return AutoMaterializeDecisionType.SKIP

    @property
    def description(self) -> str:
        return "waiting on upstream data to be present"

    def evaluate_for_asset(
        self,
        context: RuleEvaluationContext,
    ) -> RuleEvaluationResults:
        asset_partitions_by_evaluation_data = defaultdict(set)

        # only need to evaluate net-new candidates and candidates whose parents have changed
        candidates_to_evaluate = (
            context.get_candidates_not_evaluated_by_rule_on_previous_tick()
            | context.get_candidates_with_updated_or_will_update_parents()
        )
        for candidate in candidates_to_evaluate:
            missing_parent_asset_keys = set()
            for parent in context.get_parents_that_will_not_be_materialized_on_current_tick(
                asset_partition=candidate
            ):
                # ignore non-observable sources, which will never have a materialization or observation
                if context.asset_graph.is_source(
                    parent.asset_key
                ) and not context.asset_graph.is_observable(parent.asset_key):
                    continue
                if not context.instance_queryer.asset_partition_has_materialization_or_observation(
                    parent
                ):
                    missing_parent_asset_keys.add(parent.asset_key)
            if missing_parent_asset_keys:
                asset_partitions_by_evaluation_data[
                    WaitingOnAssetsRuleEvaluationData(frozenset(missing_parent_asset_keys))
                ].add(candidate)

        return self.add_evaluation_data_from_previous_tick(
            context,
            asset_partitions_by_evaluation_data,
            should_use_past_data_fn=lambda ap: ap not in candidates_to_evaluate,
        )


@whitelist_for_serdes
class SkipOnNotAllParentsUpdatedRule(
    AutoMaterializeRule,
    NamedTuple(
        "_SkipOnNotAllParentsUpdatedRule", [("require_update_for_all_parent_partitions", bool)]
    ),
):
    """An auto-materialize rule that enforces that an asset can only be materialized if all parents
    have been materialized since the asset's last materialization.

    Attributes:
        require_update_for_all_parent_partitions (Optional[bool]): Applies only to an unpartitioned
            asset or an asset partition that depends on more than one partition in any upstream asset.
            If true, requires all upstream partitions in each upstream asset to be materialized since
            the downstream asset's last materialization in order to update it. If false, requires at
            least one upstream partition in each upstream asset to be materialized since the downstream
            asset's last materialization in order to update it. Defaults to false.
    """

    @property
    def decision_type(self) -> AutoMaterializeDecisionType:
        return AutoMaterializeDecisionType.SKIP

    @property
    def description(self) -> str:
        if self.require_update_for_all_parent_partitions is False:
            return "waiting on upstream data to be updated"
        else:
            return "waiting until all upstream partitions are updated"

    def evaluate_for_asset(
        self,
        context: RuleEvaluationContext,
    ) -> RuleEvaluationResults:
        asset_partitions_by_evaluation_data = defaultdict(set)

        # only need to evaluate net-new candidates and candidates whose parents have changed
        candidates_to_evaluate = (
            context.get_candidates_not_evaluated_by_rule_on_previous_tick()
            | context.get_candidates_with_updated_or_will_update_parents()
        )
        for candidate in candidates_to_evaluate:
            parent_partitions = context.asset_graph.get_parents_partitions(
                context.instance_queryer,
                context.instance_queryer.evaluation_time,
                context.asset_key,
                candidate.partition_key,
            ).parent_partitions

            updated_parent_partitions = (
                context.instance_queryer.get_parent_asset_partitions_updated_after_child(
                    candidate,
                    parent_partitions,
                    context.daemon_context.respect_materialization_data_versions,
                    ignored_parent_keys=set(),
                )
                | set().union(
                    *[
                        context.will_materialize_mapping.get(parent, set())
                        for parent in context.asset_graph.get_parents(context.asset_key)
                    ]
                )
            )

            if self.require_update_for_all_parent_partitions:
                # All upstream partitions must be updated in order for the candidate to be updated
                non_updated_parent_keys = {
                    parent.asset_key for parent in parent_partitions - updated_parent_partitions
                }
            else:
                # At least one upstream partition in each upstream asset must be updated in order
                # for the candidate to be updated
                parent_asset_keys = context.asset_graph.get_parents(context.asset_key)
                updated_parent_partitions_by_asset_key = context.get_asset_partitions_by_asset_key(
                    updated_parent_partitions
                )
                non_updated_parent_keys = {
                    parent
                    for parent in parent_asset_keys
                    if not updated_parent_partitions_by_asset_key.get(parent)
                }

            # do not require past partitions of this asset to be updated
            non_updated_parent_keys -= {context.asset_key}

            if non_updated_parent_keys:
                asset_partitions_by_evaluation_data[
                    WaitingOnAssetsRuleEvaluationData(frozenset(non_updated_parent_keys))
                ].add(candidate)

        return self.add_evaluation_data_from_previous_tick(
            context,
            asset_partitions_by_evaluation_data,
            should_use_past_data_fn=lambda ap: ap not in candidates_to_evaluate,
        )


@whitelist_for_serdes
class SkipOnRequiredButNonexistentParentsRule(
    AutoMaterializeRule, NamedTuple("_SkipOnRequiredButNonexistentParentsRule", [])
):
    @property
    def decision_type(self) -> AutoMaterializeDecisionType:
        return AutoMaterializeDecisionType.SKIP

    @property
    def description(self) -> str:
        return "required parent partitions do not exist"

    def evaluate_for_asset(self, context: RuleEvaluationContext) -> RuleEvaluationResults:
        asset_partitions_by_evaluation_data = defaultdict(set)

        candidates_to_evaluate = context.get_candidates_not_evaluated_by_rule_on_previous_tick()
        for candidate in candidates_to_evaluate:
            nonexistent_parent_partitions = context.asset_graph.get_parents_partitions(
                context.instance_queryer,
                context.instance_queryer.evaluation_time,
                candidate.asset_key,
                candidate.partition_key,
            ).required_but_nonexistent_parents_partitions

            nonexistent_parent_keys = {parent.asset_key for parent in nonexistent_parent_partitions}
            if nonexistent_parent_keys:
                asset_partitions_by_evaluation_data[
                    WaitingOnAssetsRuleEvaluationData(frozenset(nonexistent_parent_keys))
                ].add(candidate)

        return self.add_evaluation_data_from_previous_tick(
            context,
            asset_partitions_by_evaluation_data,
            should_use_past_data_fn=lambda ap: ap not in candidates_to_evaluate,
        )


@whitelist_for_serdes
class SkipOnBackfillInProgressRule(
    AutoMaterializeRule,
    NamedTuple("_SkipOnBackfillInProgressRule", [("all_partitions", bool)]),
):
    @property
    def decision_type(self) -> AutoMaterializeDecisionType:
        return AutoMaterializeDecisionType.SKIP

    @property
    def description(self) -> str:
        if self.all_partitions:
            return "part of an asset targeted by an in-progress backfill"
        else:
            return "targeted by an in-progress backfill"

    def evaluate_for_asset(self, context: RuleEvaluationContext) -> RuleEvaluationResults:
        backfill_in_progress_candidates: AbstractSet[AssetKeyPartitionKey] = set()
        backfilling_subset = (
            context.instance_queryer.get_active_backfill_target_asset_graph_subset()
        )

        if self.all_partitions:
            backfill_in_progress_candidates = {
                candidate
                for candidate in context.candidates
                if candidate.asset_key in backfilling_subset.asset_keys
            }
        else:
            backfill_in_progress_candidates = {
                candidate for candidate in context.candidates if candidate in backfilling_subset
            }

        if backfill_in_progress_candidates:
            return [(None, backfill_in_progress_candidates)]

        return []


@whitelist_for_serdes
class DiscardOnMaxMaterializationsExceededRule(
    AutoMaterializeRule, NamedTuple("_DiscardOnMaxMaterializationsExceededRule", [("limit", int)])
):
    @property
    def decision_type(self) -> AutoMaterializeDecisionType:
        return AutoMaterializeDecisionType.DISCARD

    @property
    def description(self) -> str:
        return f"exceeds {self.limit} materialization(s) per minute"

    def evaluate_for_asset(self, context: RuleEvaluationContext) -> RuleEvaluationResults:
        # the set of asset partitions which exceed the limit
        rate_limited_asset_partitions = set(
            sorted(
                context.candidates,
                key=lambda x: sort_key_for_asset_partition(context.asset_graph, x),
            )[self.limit :]
        )
        if rate_limited_asset_partitions:
            return [(None, rate_limited_asset_partitions)]
        return []


@whitelist_for_serdes
class AutoMaterializeAssetEvaluation(NamedTuple):
    """Represents the results of the auto-materialize logic for a single asset.

    Properties:
        asset_key (AssetKey): The asset key that was evaluated.
        partition_subsets_by_condition: The rule evaluations that impact if the asset should be
            materialized, skipped, or discarded. If the asset is partitioned, this will be a list of
            tuples, where the first element is the condition and the second element is the
            serialized subset of partitions that the condition applies to. If it's not partitioned,
            the second element will be None.
        num_requested (int): The number of asset partitions that were requested to be materialized
        num_skipped (int): The number of asset partitions that were skipped
        num_discarded (int): The number of asset partitions that were discarded
        run_ids (Set[str]): The set of run IDs created for this evaluation
        rule_snapshots (Optional[Sequence[AutoMaterializeRuleSnapshot]]): The snapshots of the
            rules on the policy at the time it was evaluated.
    """

    asset_key: AssetKey
    partition_subsets_by_condition: Sequence[
        Tuple["AutoMaterializeRuleEvaluation", Optional[SerializedPartitionsSubset]]
    ]
    num_requested: int
    num_skipped: int
    num_discarded: int
    run_ids: Set[str] = set()
    rule_snapshots: Optional[Sequence[AutoMaterializeRuleSnapshot]] = None

    @staticmethod
    def from_rule_evaluation_results(
        asset_graph: AssetGraph,
        asset_key: AssetKey,
        asset_partitions_by_rule_evaluation: Sequence[
            Tuple[AutoMaterializeRuleEvaluation, AbstractSet[AssetKeyPartitionKey]]
        ],
        num_requested: int,
        num_skipped: int,
        num_discarded: int,
        dynamic_partitions_store: "DynamicPartitionsStore",
    ) -> "AutoMaterializeAssetEvaluation":
        auto_materialize_policy = asset_graph.auto_materialize_policies_by_key.get(asset_key)

        if not auto_materialize_policy:
            check.failed(f"Expected auto materialize policy on asset {asset_key}")

        partitions_def = asset_graph.get_partitions_def(asset_key)
        if partitions_def is None:
            return AutoMaterializeAssetEvaluation(
                asset_key=asset_key,
                partition_subsets_by_condition=[
                    (rule_evaluation, None)
                    for rule_evaluation, _ in asset_partitions_by_rule_evaluation
                ],
                num_requested=num_requested,
                num_skipped=num_skipped,
                num_discarded=num_discarded,
                rule_snapshots=auto_materialize_policy.rule_snapshots,
            )
        else:
            return AutoMaterializeAssetEvaluation(
                asset_key=asset_key,
                partition_subsets_by_condition=[
                    (
                        rule_evaluation,
                        SerializedPartitionsSubset.from_subset(
                            subset=partitions_def.empty_subset().with_partition_keys(
                                check.not_none(ap.partition_key) for ap in asset_partitions
                            ),
                            partitions_def=partitions_def,
                            dynamic_partitions_store=dynamic_partitions_store,
                        ),
                    )
                    for rule_evaluation, asset_partitions in asset_partitions_by_rule_evaluation
                ],
                num_requested=num_requested,
                num_skipped=num_skipped,
                num_discarded=num_discarded,
                rule_snapshots=auto_materialize_policy.rule_snapshots,
            )

    def _deserialize_rule_evaluation_result(
        self,
        rule_evaluation: AutoMaterializeRuleEvaluation,
        serialized_subset: Optional[SerializedPartitionsSubset],
        asset_graph: AssetGraph,
    ) -> Optional[
        Tuple[Optional[AutoMaterializeRuleEvaluationData], AbstractSet[AssetKeyPartitionKey]]
    ]:
        partitions_def = asset_graph.get_partitions_def(self.asset_key)
        if serialized_subset is None:
            if partitions_def is None:
                return (rule_evaluation.evaluation_data, {AssetKeyPartitionKey(self.asset_key)})
        elif serialized_subset.can_deserialize(partitions_def) and partitions_def is not None:
            return (
                rule_evaluation.evaluation_data,
                {
                    AssetKeyPartitionKey(self.asset_key, partition_key)
                    for partition_key in serialized_subset.deserialize(
                        partitions_def=partitions_def
                    ).get_partition_keys()
                },
            )
        # old serialized result is no longer valid
        return None

    def get_rule_evaluation_results(
        self, rule_snapshot: AutoMaterializeRuleSnapshot, asset_graph: AssetGraph
    ) -> RuleEvaluationResults:
        """For a given rule snapshot, returns the calculated evaluations for that rule."""
        results = []
        for rule_evaluation, serialized_subset in self.partition_subsets_by_condition:
            # filter for the same rule
            if rule_evaluation.rule_snapshot != rule_snapshot:
                continue
            deserialized_result = self._deserialize_rule_evaluation_result(
                rule_evaluation, serialized_subset, asset_graph
            )
            if deserialized_result:
                results.append(deserialized_result)
        return results

    def _get_asset_partitions_with_decision_type(
        self, decision_type: AutoMaterializeDecisionType, asset_graph: AssetGraph
    ) -> AbstractSet[AssetKeyPartitionKey]:
        """Returns the set of asset partitions with a given decision type applied to them."""
        asset_partitions = set()
        for rule_evaluation, serialized_subset in self.partition_subsets_by_condition:
            if rule_evaluation.rule_snapshot.decision_type != decision_type:
                continue
            deserialized_result = self._deserialize_rule_evaluation_result(
                rule_evaluation, serialized_subset, asset_graph
            )
            if deserialized_result is None:
                continue
            asset_partitions.update(deserialized_result[1])
        return asset_partitions

    def get_requested_or_discarded_asset_partitions(
        self, asset_graph: AssetGraph
    ) -> AbstractSet[AssetKeyPartitionKey]:
        """Returns the set of asset partitions which were either requested or discarded on this
        evaluation.
        """
        to_materialize = self._get_asset_partitions_with_decision_type(
            AutoMaterializeDecisionType.MATERIALIZE, asset_graph
        )
        if not to_materialize:
            return set()
        to_skip = self._get_asset_partitions_with_decision_type(
            AutoMaterializeDecisionType.SKIP, asset_graph
        )
        return to_materialize - to_skip

    def get_evaluated_asset_partitions(
        self, asset_graph: AssetGraph
    ) -> AbstractSet[AssetKeyPartitionKey]:
        """Returns the set of asset partitions which were evaluated by any rule on this evaluation."""
        # no asset partition can be evaluated by SKIP or DISCARD rules without having at least one
        # materialize rule evaluation
        return self._get_asset_partitions_with_decision_type(
            AutoMaterializeDecisionType.MATERIALIZE, asset_graph
        )

    def equivalent_to_stored_evaluation(
        self, stored_evaluation: Optional["AutoMaterializeAssetEvaluation"], asset_graph: AssetGraph
    ) -> bool:
        """This function returns if a stored record is equivalent to this one. To do so, we can't
        just use regular namedtuple equality, as the serialized partition subsets will be
        potentially have different string values.
        """
        if stored_evaluation is None:
            return False
        sorted_results = sorted(self.partition_subsets_by_condition)
        sorted_stored_results = sorted(stored_evaluation.partition_subsets_by_condition)
        return (
            self.asset_key == stored_evaluation.asset_key
            and set(self.rule_snapshots or []) == set(stored_evaluation.rule_snapshots or [])
            # if num_requested / num_discarded > 0 on the stored evaluation, then something changed
            # in the global state on the previous tick
            and stored_evaluation.num_requested == 0
            and stored_evaluation.num_discarded == 0
            and stored_evaluation.num_skipped == self.num_skipped
            and len(sorted_results) == len(sorted_stored_results)
            and (
                # first is a quick check for the equality of the string representations of the
                # partition subsets
                sorted_results == [tuple(x) for x in sorted_stored_results]
                # however, not all identical partition subsets are serialized to the same string,
                # so sometimes we need to deserialize the keys to be sure
                or [
                    self._deserialize_rule_evaluation_result(re, ss, asset_graph)
                    for re, ss in sorted_results
                ]
                == [
                    self._deserialize_rule_evaluation_result(re, ss, asset_graph)
                    for re, ss in sorted_stored_results
                ]
            )
        )


# BACKCOMPAT GRAVEYARD


class BackcompatAutoMaterializeConditionSerializer(NamedTupleSerializer):
    """This handles backcompat for the old AutoMaterializeCondition objects, turning them into the
    proper AutoMaterializeRuleEvaluation objects. This is necessary because old
    AutoMaterializeAssetEvaluation objects will have serialized AutoMaterializeCondition objects,
    and we need to be able to deserialize them.

    In theory, as these serialized objects happen to be purged periodically, we can remove this
    backcompat logic at some point in the future.
    """

    def unpack(
        self,
        unpacked_dict: Dict[str, UnpackedValue],
        whitelist_map: WhitelistMap,
        context: UnpackContext,
    ) -> AutoMaterializeRuleEvaluation:
        if self.klass in (
            FreshnessAutoMaterializeCondition,
            DownstreamFreshnessAutoMaterializeCondition,
        ):
            return AutoMaterializeRuleEvaluation(
                rule_snapshot=AutoMaterializeRule.materialize_on_required_for_freshness().to_snapshot(),
                evaluation_data=None,
            )
        elif self.klass == MissingAutoMaterializeCondition:
            return AutoMaterializeRuleEvaluation(
                rule_snapshot=AutoMaterializeRule.materialize_on_missing().to_snapshot(),
                evaluation_data=None,
            )
        elif self.klass == ParentMaterializedAutoMaterializeCondition:
            updated_asset_keys = unpacked_dict.get("updated_asset_keys")
            if isinstance(updated_asset_keys, set):
                updated_asset_keys = cast(FrozenSet[AssetKey], frozenset(updated_asset_keys))
            else:
                updated_asset_keys = frozenset()
            will_update_asset_keys = unpacked_dict.get("will_update_asset_keys")
            if isinstance(will_update_asset_keys, set):
                will_update_asset_keys = cast(
                    FrozenSet[AssetKey], frozenset(will_update_asset_keys)
                )
            else:
                will_update_asset_keys = frozenset()
            return AutoMaterializeRuleEvaluation(
                rule_snapshot=AutoMaterializeRule.materialize_on_parent_updated().to_snapshot(),
                evaluation_data=ParentUpdatedRuleEvaluationData(
                    updated_asset_keys=updated_asset_keys,
                    will_update_asset_keys=will_update_asset_keys,
                ),
            )
        elif self.klass == ParentOutdatedAutoMaterializeCondition:
            waiting_on_asset_keys = unpacked_dict.get("waiting_on_asset_keys")
            if isinstance(waiting_on_asset_keys, set):
                waiting_on_asset_keys = cast(FrozenSet[AssetKey], frozenset(waiting_on_asset_keys))
            else:
                waiting_on_asset_keys = frozenset()
            return AutoMaterializeRuleEvaluation(
                rule_snapshot=AutoMaterializeRule.skip_on_parent_outdated().to_snapshot(),
                evaluation_data=WaitingOnAssetsRuleEvaluationData(
                    waiting_on_asset_keys=waiting_on_asset_keys
                ),
            )
        elif self.klass == MaxMaterializationsExceededAutoMaterializeCondition:
            return AutoMaterializeRuleEvaluation(
                rule_snapshot=DiscardOnMaxMaterializationsExceededRule(limit=1).to_snapshot(),
                evaluation_data=None,
            )
        check.failed(f"Unexpected class {self.klass}")


@whitelist_for_serdes(serializer=BackcompatAutoMaterializeConditionSerializer)
class FreshnessAutoMaterializeCondition(NamedTuple):
    ...


@whitelist_for_serdes(serializer=BackcompatAutoMaterializeConditionSerializer)
class DownstreamFreshnessAutoMaterializeCondition(NamedTuple):
    ...


@whitelist_for_serdes(serializer=BackcompatAutoMaterializeConditionSerializer)
class ParentMaterializedAutoMaterializeCondition(NamedTuple):
    ...


@whitelist_for_serdes(serializer=BackcompatAutoMaterializeConditionSerializer)
class MissingAutoMaterializeCondition(NamedTuple):
    ...


@whitelist_for_serdes(serializer=BackcompatAutoMaterializeConditionSerializer)
class ParentOutdatedAutoMaterializeCondition(NamedTuple):
    ...


@whitelist_for_serdes(serializer=BackcompatAutoMaterializeConditionSerializer)
class MaxMaterializationsExceededAutoMaterializeCondition(NamedTuple):
    ...
