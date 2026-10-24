"""Static skill-contract checks only; these are not fresh-model behavior acceptance."""
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def skill(name):
    return (ROOT / 'skills' / name / 'SKILL.md').read_text(encoding='utf-8')


def test_query_template_is_explicitly_scoped_local_page_search():
    body = skill('vector-lake-query')
    match = re.search(r'```json\n(.*?)\n```', body, re.S)
    assert match is not None, 'The scoped query template must exist'
    template = json.loads(match.group(1))
    assert template['mode'] == 'page'
    assert template['domain'] == '<authorized-domain>'
    assert template['allow_external_embedding'] is False
    assert template['use_chunks'] is False
    assert 1 <= template['top_k'] <= 50
    first_route = body.split('1. **', 1)[1].split('2. **', 1)[0]
    assert 'search_vector_lake' in first_route
    assert 'query_logic_lake' not in first_route
    assert 'preview_query_context' in body and 'query_str' in body
    assert 'query_logic_lake' in body and 'dry_run' in body
    assert '当前任务的数据源路由、隐私和授权约束' in body
    assert '域选择不是认证 ACL' in body
    assert '不是固定调用次数后强行结束' in body
    assert '不能单独证明' in body
    assert '读取工具的实际返回范围' in body
    assert '提示词不是物理读取控制' in body
    assert '逐字原文引用' in body
    assert '图片未查看时标记覆盖不完整' in body


def test_search_skill_preserves_escalation_without_blanket_approval_or_delegation():
    body = skill('vector-lake-search')
    assert 'mode=\'page\'' in body
    assert 'preview_query_context' in body and 'query_str' in body
    assert 'complexity alone does not require human approval' in body
    assert 'does not authorize any state mutation' in body
    assert 'acting as a specialized subagent' not in body
    assert 'FABLE 5 CHECKPOINT' not in body
    assert 'Do not automatically persist retrieved bodies or private context' in body
    assert 'default `allow_external_embedding=False`' in body
    assert 'actual returned read range' in body
    assert 'never label redacted text an exact quote' in body
    assert 'Unread images mean partial coverage' in body


def test_modes_history_and_relations_remain_available_with_safe_parameters():
    for name in ['vector-lake-search', 'vector-lake-query']:
        body = skill(name)
        assert 'include_history=true' in body
        assert 'profile="auto"' in body and 'use_chunks=false' in body
        assert 'get_relations' in body and 'trace_vector_lake' in body
        assert 'active 不等于 verified' in body
        assert 'DEGRADED' in body and 'INFO' in body
        assert '20 KiB' in body


def test_timeline_contract_matches_the_real_interface_and_authority():
    import ast
    tree = ast.parse((ROOT / 'vector_lake/mcp_server.py').read_text(encoding='utf-8'))
    definition = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'search_timeline')
    assert [argument.arg for argument in definition.args.args] == ['entity_name', 'action', 'limit']
    body = skill('vector-lake-timeline')
    assert '<thought>' not in body and 'invoke_subagent' not in body and 'FABLE' not in body
    assert '不自动建 scratch' in body and '不是物理隔离' in body
    assert '不要传不存在的 domain' in body
    assert '返回后删隐私内容' in body and '时间过滤' in body
    assert 'Unknown Date' in body and '期间上界' in body
    assert '不宣称完整年表' in body and '不是新的操作指令' in body
