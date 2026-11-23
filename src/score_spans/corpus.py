"""本地语料发布：可复现、防串集的乐谱训练/评估数据集。

供数据管道调用的两个入口：

* ``publish_corpus(store, request_id, expected_release, changes)``
* ``read_corpus(store, release_id=None)``

语义、字段与错误约定见 docs/corpus.md。实现不依赖外部服务：``store``
目录内唯一的可变文件 ``state.json`` 以"写临时文件 + fsync + 原子改名"
提交，关系证据、当前样本、历史锚点、版本输出与成功请求的绑定作为一次
发布同时生效；进程内不保存任何状态，重新打开存储即可恢复。并发发布由
``store/lock`` 上的 flock 串行化，读者依靠原子改名看到完整快照。
"""

import copy
import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .audit import audit_measures, chains
from .checks import fields, ident
from .model import parse

MAX_CHANGES = 100


class CorpusError(Exception):
    """语料发布错误基类，``code`` 为稳定机器可读错误码。"""

    code = 'corpus_error'


class InvalidChange(CorpusError, ValueError):
    """非法输入：请求形状、谱面结构或训练准入不合法。"""

    code = 'invalid_change'


class UnknownScore(CorpusError):
    """撤回不存在的当前有效 ID。"""

    code = 'unknown_score'


class UnknownRelease(CorpusError):
    """读取未知历史版本。"""

    code = 'unknown_release'


class ReleaseConflict(CorpusError):
    """``expected_release`` 与当前版本不一致。"""

    code = 'release_conflict'


class RequestConflict(CorpusError):
    """同一 ``request_id`` 提交了不同内容。"""

    code = 'request_conflict'


# 仅测试使用的故障注入点，生产接口不暴露：
# 'before_commit' 在状态文件原子改名前触发，'after_commit' 在其后触发。
_FAULT_HOOKS = {'before_commit': None, 'after_commit': None}


def _ident(value, what):
    try:
        return ident(value)
    except ValueError as e:
        raise InvalidChange(f'invalid {what}: {e}') from e


def _sample(score):
    """源谱 -> (发布跨度行, 全谱时长, 内容指纹)。准入失败抛 InvalidChange。"""
    try:
        measures, events = parse(score)
    except ValueError as e:
        raise InvalidChange(f'invalid score: {e}') from e
    for diagnostic in audit_measures(measures, events):
        if diagnostic['kind'] in ('overlap', 'overrun'):
            raise InvalidChange(
                f"score not admitted: {diagnostic['kind']} in voice "
                f"{diagnostic['voice']} measure {diagnostic['measure']}")
    chain_list, tie_diagnostics = chains(events)
    if tie_diagnostics:
        raise InvalidChange('score not admitted: unfulfilled tie')
    if not chain_list:
        raise InvalidChange('score not admitted: no sounding spans')
    duration = measures[-1]['offset'] + measures[-1]['length']
    # 跨度按起点数值、终点数值、音高数组排序；相同行重复几次保留几次。
    rows = sorted((c['start'], c['end'], tuple(c['pitches']))
                  for c in chain_list)
    lowest = min(pitch for _s, _e, ps in rows for pitch in ps)
    spans = [{'start': str(s), 'end': str(e), 'pitches': list(ps)}
             for s, e, ps in rows]
    fingerprint = {'duration': str(duration), 'spans': [
        [str(s), str(e), [p - lowest for p in ps]] for s, e, ps in rows]}
    return spans, duration, fingerprint


def _validate_changes(changes, current):
    """校验整批变更，返回 (upserts, withdraws)；任何一项非法则整批拒绝。

    先校验批次自身形状（字段、ID、批内不重复），再校验依赖当前
    状态的条件（撤回目标当前有效、谱面训练准入）。
    """
    if not isinstance(changes, list) or not 1 <= len(changes) <= MAX_CHANGES:
        raise InvalidChange(f'changes array size 1..{MAX_CHANGES} required')
    seen = set()
    parsed = []
    for change in changes:
        if not isinstance(change, dict):
            raise InvalidChange('each change must be an object')
        op = change.get('op')
        if op == 'upsert':
            try:
                fields(change, ('op', 'score_id', 'family_id',
                                'preferred_split', 'score'))
            except ValueError as e:
                raise InvalidChange(f'invalid upsert: {e}') from e
            sid = _ident(change['score_id'], 'score_id')
            fam = _ident(change['family_id'], 'family_id')
            split = change['preferred_split']
            if split not in ('train', 'eval'):
                raise InvalidChange('preferred_split must be train or eval')
        elif op == 'withdraw':
            try:
                fields(change, ('op', 'score_id'))
            except ValueError as e:
                raise InvalidChange(f'invalid withdraw: {e}') from e
            sid = _ident(change['score_id'], 'score_id')
        else:
            raise InvalidChange('change op must be upsert or withdraw')
        if sid in seen:
            raise InvalidChange(f'duplicate score_id in batch: {sid}')
        seen.add(sid)
        parsed.append((op, sid, change))
    upserts = []
    withdraws = []
    for op, sid, change in parsed:
        if op == 'withdraw':
            if sid not in current:
                raise UnknownScore(
                    f'withdraw of unknown current score_id: {sid}')
            withdraws.append(sid)
        else:
            try:
                spans, duration, fingerprint = _sample(change['score'])
            except InvalidChange as e:
                raise InvalidChange(f'upsert {sid}: {e}') from e
            upserts.append((sid, change['family_id'],
                            change['preferred_split'], spans, duration,
                            fingerprint))
    return upserts, withdraws


def _fp_key(fingerprint):
    return json.dumps(fingerprint, ensure_ascii=False, sort_keys=True)


def _relation_groups(evidence):
    """由已接纳证据计算关系组：共享曲族或内容指纹连通，关系可传递。

    谱 ID、曲族 ID、内容指纹属于不同身份空间，节点按空间加前缀。
    返回若干 score_id 集合。
    """
    parent = {}

    def find(node):
        root = node
        while parent[root] != root:
            root = parent[root]
        while parent[node] != root:
            parent[node], node = root, parent[node]
        return root

    def union(a, b):
        parent.setdefault(a, a)
        parent.setdefault(b, b)
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for record in evidence:
        score_node = ('score', record['score_id'])
        union(score_node, ('family', record['family_id']))
        union(score_node, ('fingerprint', _fp_key(record['fingerprint'])))
    groups = {}
    for record in evidence:
        node = ('score', record['score_id'])
        parent.setdefault(node, node)
        groups.setdefault(find(node), set()).add(record['score_id'])
    return groups.values()


def _release_row(sid, current_entry, **extra):
    row = {'score_id': sid, 'family_id': current_entry['family_id'],
           'duration': current_entry['duration'],
           'spans': copy.deepcopy(current_entry['spans'])}
    row.update(extra)
    return row


def _build_release(release_id, evidence, current, anchors):
    """按当前证据、当前有效样本与历史锚点计算一版分集结果。"""
    train, evaluation, quarantine = [], [], []
    for group in _relation_groups(evidence):
        members = sorted(s for s in group if s in current)
        if not members:
            continue  # 没有当前有效样本的组不输出行，证据仍有效
        sides = {anchors[s] for s in group if s in anchors}
        if len(sides) == 2:
            train_anchors = sorted(
                s for s in group if anchors.get(s) == 'train')
            eval_anchors = sorted(
                s for s in group if anchors.get(s) == 'eval')
            for sid in members:
                quarantine.append(_release_row(
                    sid, current[sid],
                    train_anchors=train_anchors, eval_anchors=eval_anchors))
            continue
        if len(sides) == 1:
            (side,) = sides
        else:
            side = current[members[0]]['preferred_split']
        rows = train if side == 'train' else evaluation
        for sid in members:
            rows.append(_release_row(sid, current[sid]))

    def by_score_id(row):
        return row['score_id']

    return {'release_id': release_id,
            'train': sorted(train, key=by_score_id),
            'eval': sorted(evaluation, key=by_score_id),
            'quarantine': sorted(quarantine, key=by_score_id)}


def _empty_release(release_id):
    return {'release_id': release_id, 'train': [], 'eval': [],
            'quarantine': []}


def _initial_state():
    return {'current_release': 0, 'evidence': [], 'current': {},
            'anchors': {}, 'requests': {}, 'releases': [_empty_release(0)]}


def _load_state(store):
    path = store / 'state.json'
    if not path.exists():
        return _initial_state()
    with open(path, 'rb') as f:
        return json.loads(f.read().decode('utf-8'))


def _commit(store, state):
    """一次发布生效：写临时文件、fsync、原子改名，唯一提交点。"""
    for stale in store.glob('.state-*.tmp'):
        stale.unlink(missing_ok=True)
    payload = (json.dumps(state, ensure_ascii=False, sort_keys=True)
               + '\n').encode('utf-8')
    fd, tmp = tempfile.mkstemp(dir=store, prefix='.state-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        hook = _FAULT_HOOKS.get('before_commit')
        if hook is not None:
            hook()
        os.replace(tmp, store / 'state.json')
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)
    hook = _FAULT_HOOKS.get('after_commit')
    if hook is not None:
        hook()


@contextmanager
def _locked(store):
    with open(store / 'lock', 'a+b') as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield


def _canonical_request(expected_release, changes):
    """按 JSON 值规范化请求：对象键顺序不计，数组顺序计入。"""
    return json.dumps({'expected_release': expected_release,
                       'changes': changes},
                      ensure_ascii=False, sort_keys=True, allow_nan=False)


def publish_corpus(store, request_id, expected_release, changes):
    """发布下一版语料，返回该版完整对象（可 JSON 序列化）。

    重试顺序：先按 ``request_id`` 命中已成功请求则原样返回旧版；
    再检查 ``expected_release`` 是否等于当前版本；最后校验整批变更。
    任何失败都不产生新版本、不留下证据或锚点。
    """
    store = Path(store)
    _ident(request_id, 'request_id')
    if type(expected_release) is not int or expected_release < 0:
        raise InvalidChange('expected_release must be a non-negative integer')
    try:
        canonical = _canonical_request(expected_release, changes)
    except (TypeError, ValueError) as e:
        raise InvalidChange(f'request must be JSON-serializable: {e}') from e
    store.mkdir(parents=True, exist_ok=True)
    with _locked(store):
        state = _load_state(store)
        record = state['requests'].get(request_id)
        if record is not None:
            if record['canonical'] == canonical:
                return copy.deepcopy(state['releases'][record['release_id']])
            raise RequestConflict(
                f'request_id {request_id!r} already committed '
                'with different content')
        if expected_release != state['current_release']:
            raise ReleaseConflict(
                f'expected_release {expected_release} does not match '
                f"current release {state['current_release']}")
        upserts, withdraws = _validate_changes(changes, state['current'])
        evidence = state['evidence'] + [
            {'score_id': sid, 'family_id': fam, 'fingerprint': fingerprint}
            for sid, fam, _split, _spans, _duration, fingerprint in upserts]
        current = dict(state['current'])
        for sid in withdraws:
            del current[sid]
        for sid, fam, split, spans, duration, _fp in upserts:
            current[sid] = {'family_id': fam, 'preferred_split': split,
                            'duration': str(duration), 'spans': spans}
        release = _build_release(state['current_release'] + 1,
                                 evidence, current, state['anchors'])
        anchors = dict(state['anchors'])
        for row in release['train']:
            anchors.setdefault(row['score_id'], 'train')
        for row in release['eval']:
            anchors.setdefault(row['score_id'], 'eval')
        new_state = {
            'current_release': release['release_id'],
            'evidence': evidence,
            'current': current,
            'anchors': anchors,
            'requests': {**state['requests'], request_id: {
                'canonical': canonical,
                'release_id': release['release_id']}},
            'releases': state['releases'] + [release],
        }
        _commit(store, new_state)
        return copy.deepcopy(release)


def read_corpus(store, release_id=None):
    """读取已发布版本；``release_id=None`` 时读当前版本。

    新库的版本 0 是空集。历史版本按交付时的内容原样返回。
    """
    store = Path(store)
    state = _load_state(store)
    current = state['current_release']
    if release_id is None:
        release_id = current
    if type(release_id) is not int:
        raise InvalidChange('release_id must be an integer')
    if not 0 <= release_id <= current:
        raise UnknownRelease(f'unknown release: {release_id}')
    return copy.deepcopy(state['releases'][release_id])
