"""State differ producing strongly-typed canonical Effect objects."""

from __future__ import annotations
import json
from typing import Any, Dict, List, Set, Tuple
from evoundo.core.state import HarnessState
from evoundo.effects.contracts import Effect, EffectCategory, EffectOpType


class StateDiffer:
    """Computes exact, canonical semantic differences between two HarnessState instances."""

    @classmethod
    def diff(cls, pre_state: HarnessState, post_state: HarnessState) -> List[Effect]:
        """Compute all observed semantic effects from pre_state to post_state."""
        effects: List[Effect] = []

        # 1. Config Surface
        effects.extend(cls._diff_config(pre_state.config, post_state.config))

        # 2. Tools Surface
        effects.extend(cls._diff_tools(pre_state.tools, post_state.tools))

        # 3. Middleware Surface
        effects.extend(cls._diff_middleware(pre_state.middleware, post_state.middleware))

        # 4. Event Listeners Surface
        effects.extend(cls._diff_listeners(pre_state.event_listeners, post_state.event_listeners))

        # 5. Files Surface
        effects.extend(cls._diff_files(pre_state.files, post_state.files))

        # 6. Resources Surface
        effects.extend(cls._diff_resources(pre_state.resources, post_state.resources))

        # 7. Prompts Surface
        effects.extend(cls._diff_prompts(pre_state.prompts, post_state.prompts))

        return effects

    @classmethod
    def _diff_config(cls, pre: Dict[str, Any], post: Dict[str, Any]) -> List[Effect]:
        effects: List[Effect] = []
        all_keys = set(pre.keys()).union(post.keys())
        for key in sorted(all_keys):
            if key not in pre and key in post:
                effects.append(Effect(
                    category=EffectCategory.CONFIG,
                    target=key,
                    op_type=EffectOpType.CREATE,
                    old_value=None,
                    new_value=post[key],
                ))
            elif key in pre and key not in post:
                effects.append(Effect(
                    category=EffectCategory.CONFIG,
                    target=key,
                    op_type=EffectOpType.DELETE,
                    old_value=pre[key],
                    new_value=None,
                ))
            elif pre[key] != post[key]:
                effects.append(Effect(
                    category=EffectCategory.CONFIG,
                    target=key,
                    op_type=EffectOpType.UPDATE,
                    old_value=pre[key],
                    new_value=post[key],
                ))
        return effects

    @classmethod
    def _diff_tools(cls, pre: Dict[str, Any], post: Dict[str, Any]) -> List[Effect]:
        effects: List[Effect] = []
        all_tools = set(pre.keys()).union(post.keys())
        for name in sorted(all_tools):
            if name not in pre and name in post:
                effects.append(Effect(
                    category=EffectCategory.TOOLS,
                    target=name,
                    op_type=EffectOpType.CREATE,
                    old_value=None,
                    new_value=post[name].canonical_dict(),
                ))
            elif name in pre and name not in post:
                effects.append(Effect(
                    category=EffectCategory.TOOLS,
                    target=name,
                    op_type=EffectOpType.DELETE,
                    old_value=pre[name].canonical_dict(),
                    new_value=None,
                ))
            else:
                pre_dict = pre[name].canonical_dict()
                post_dict = post[name].canonical_dict()
                if pre_dict != post_dict or pre[name].fn != post[name].fn:
                    effects.append(Effect(
                        category=EffectCategory.TOOLS,
                        target=name,
                        op_type=EffectOpType.UPDATE,
                        old_value=pre_dict,
                        new_value=post_dict,
                    ))
        return effects

    @classmethod
    def _diff_middleware(cls, pre_list: list, post_list: list) -> List[Effect]:
        effects: List[Effect] = []
        pre_map = {m.id: m for m in pre_list}
        post_map = {m.id: m for m in post_list}
        all_ids = set(pre_map.keys()).union(post_map.keys())

        for mid in sorted(all_ids):
            if mid not in pre_map and mid in post_map:
                effects.append(Effect(
                    category=EffectCategory.MIDDLEWARE,
                    target=mid,
                    op_type=EffectOpType.CREATE,
                    old_value=None,
                    new_value=post_map[mid].canonical_dict(),
                ))
            elif mid in pre_map and mid not in post_map:
                effects.append(Effect(
                    category=EffectCategory.MIDDLEWARE,
                    target=mid,
                    op_type=EffectOpType.DELETE,
                    old_value=pre_map[mid].canonical_dict(),
                    new_value=None,
                ))
            else:
                pre_m = pre_map[mid]
                post_m = post_map[mid]
                if pre_m.canonical_dict() != post_m.canonical_dict() or pre_m.fn != post_m.fn:
                    effects.append(Effect(
                        category=EffectCategory.MIDDLEWARE,
                        target=mid,
                        op_type=EffectOpType.UPDATE,
                        old_value=pre_m.canonical_dict(),
                        new_value=post_m.canonical_dict(),
                    ))

        # Check sequence order change among surviving elements
        common_ids = [m.id for m in pre_list if m.id in post_map]
        post_common_ids = [m.id for m in post_list if m.id in pre_map]
        if common_ids != post_common_ids:
            for mid in post_common_ids:
                if (EffectCategory.MIDDLEWARE, mid) not in [(e.category, e.target) for e in effects]:
                    effects.append(Effect(
                        category=EffectCategory.MIDDLEWARE,
                        target=mid,
                        op_type=EffectOpType.REORDER,
                        old_value=common_ids,
                        new_value=post_common_ids,
                    ))
        return effects

    @classmethod
    def _diff_listeners(cls, pre: Dict[str, list], post: Dict[str, list]) -> List[Effect]:
        effects: List[Effect] = []
        all_events = set(pre.keys()).union(post.keys())

        for event in sorted(all_events):
            pre_listeners = {l.id: l for l in pre.get(event, [])}
            post_listeners = {l.id: l for l in post.get(event, [])}
            all_lids = set(pre_listeners.keys()).union(post_listeners.keys())

            for lid in sorted(all_lids):
                target_key = f"{event}:{lid}"
                if lid not in pre_listeners and lid in post_listeners:
                    effects.append(Effect(
                        category=EffectCategory.EVENT_LISTENERS,
                        target=target_key,
                        op_type=EffectOpType.CREATE,
                        old_value=None,
                        new_value=post_listeners[lid].canonical_dict(),
                    ))
                elif lid in pre_listeners and lid not in post_listeners:
                    effects.append(Effect(
                        category=EffectCategory.EVENT_LISTENERS,
                        target=target_key,
                        op_type=EffectOpType.DELETE,
                        old_value=pre_listeners[lid].canonical_dict(),
                        new_value=None,
                    ))
                else:
                    pre_l = pre_listeners[lid]
                    post_l = post_listeners[lid]
                    if pre_l.canonical_dict() != post_l.canonical_dict() or pre_l.callback != post_l.callback:
                        effects.append(Effect(
                            category=EffectCategory.EVENT_LISTENERS,
                            target=target_key,
                            op_type=EffectOpType.UPDATE,
                            old_value=pre_l.canonical_dict(),
                            new_value=post_l.canonical_dict(),
                        ))
        return effects

    @classmethod
    def _diff_files(cls, pre: Dict[str, Any], post: Dict[str, Any]) -> List[Effect]:
        effects: List[Effect] = []
        all_files = set(pre.keys()).union(post.keys())

        for path in sorted(all_files):
            pre_f = pre.get(path)
            post_f = post.get(path)

            pre_active = pre_f is not None and not pre_f.is_deleted
            post_active = post_f is not None and not post_f.is_deleted

            if not pre_active and post_active:
                effects.append(Effect(
                    category=EffectCategory.FILES,
                    target=path,
                    op_type=EffectOpType.CREATE,
                    old_value=None,
                    new_value=post_f.content,
                ))
            elif pre_active and not post_active:
                effects.append(Effect(
                    category=EffectCategory.FILES,
                    target=path,
                    op_type=EffectOpType.DELETE,
                    old_value=pre_f.content,
                    new_value=None,
                ))
            elif pre_active and post_active:
                if pre_f.content != post_f.content or pre_f.mode != post_f.mode:
                    effects.append(Effect(
                        category=EffectCategory.FILES,
                        target=path,
                        op_type=EffectOpType.UPDATE,
                        old_value=pre_f.content,
                        new_value=post_f.content,
                    ))
        return effects

    @classmethod
    def _diff_resources(cls, pre: Dict[str, Any], post: Dict[str, Any]) -> List[Effect]:
        effects: List[Effect] = []
        all_res = set(pre.keys()).union(post.keys())

        for rid in sorted(all_res):
            pre_r = pre.get(rid)
            post_r = post.get(rid)

            if pre_r is None and post_r is not None:
                effects.append(Effect(
                    category=EffectCategory.RESOURCES,
                    target=rid,
                    op_type=EffectOpType.CREATE,
                    old_value=None,
                    new_value=post_r.canonical_dict(),
                ))
            elif pre_r is not None and post_r is None:
                effects.append(Effect(
                    category=EffectCategory.RESOURCES,
                    target=rid,
                    op_type=EffectOpType.DELETE,
                    old_value=pre_r.canonical_dict(),
                    new_value=None,
                ))
            elif pre_r is not None and post_r is not None:
                if pre_r.canonical_dict() != post_r.canonical_dict() or pre_r.state != post_r.state:
                    effects.append(Effect(
                        category=EffectCategory.RESOURCES,
                        target=rid,
                        op_type=EffectOpType.UPDATE,
                        old_value=pre_r.canonical_dict(),
                        new_value=post_r.canonical_dict(),
                    ))
        return effects

    @classmethod
    def _diff_prompts(cls, pre: Dict[str, str], post: Dict[str, str]) -> List[Effect]:
        effects: List[Effect] = []
        all_prompts = set(pre.keys()).union(post.keys())

        for name in sorted(all_prompts):
            if name not in pre and name in post:
                effects.append(Effect(
                    category=EffectCategory.PROMPTS,
                    target=name,
                    op_type=EffectOpType.CREATE,
                    old_value=None,
                    new_value=post[name],
                ))
            elif name in pre and name not in post:
                effects.append(Effect(
                    category=EffectCategory.PROMPTS,
                    target=name,
                    op_type=EffectOpType.DELETE,
                    old_value=pre[name],
                    new_value=None,
                ))
            elif pre[name] != post[name]:
                effects.append(Effect(
                    category=EffectCategory.PROMPTS,
                    target=name,
                    op_type=EffectOpType.UPDATE,
                    old_value=pre[name],
                    new_value=post[name],
                ))
        return effects
