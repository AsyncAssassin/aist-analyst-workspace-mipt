#!/usr/bin/env python3
"""Offline validation of the API contract and examples, without a product runtime."""
import argparse
import hashlib
import importlib.metadata
import json
import platform
import re
import subprocess
import sys
import unicodedata
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from fractions import Fraction
from pathlib import Path
from urllib.parse import unquote, urldefrag

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parent
COUNTS = {}


def require(test, message):
    if not test:
        raise AssertionError(message)


def pairs(values):
    result = {}
    for key, value in values:
        require(key not in result, 'Duplicate JSON key: ' + key)
        result[key] = value
    return result


def read(path):
    return json.loads(path.read_text(), object_pairs_hook=pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))


def canonical(value):
    if isinstance(value, dict):
        normalized = {}
        for key, item in value.items():
            new_key = unicodedata.normalize('NFC', key)
            require(new_key not in normalized, 'Duplicate normalized key')
            normalized[new_key] = canonical(item)
        return normalized
    if isinstance(value, list):
        return [canonical(item) for item in value]
    if isinstance(value, str):
        return unicodedata.normalize('NFC', value.replace('\r\n', '\n').replace('\r', '\n'))
    return value


def digest(value):
    return hashlib.sha256(json.dumps(canonical(value), ensure_ascii=False,
                                    sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()


def pointer(doc, fragment):
    if not fragment:
        return doc
    require(fragment.startswith('/'), 'Only JSON pointers are supported')
    for part in fragment[1:].split('/'):
        part = unquote(part).replace('~1', '/').replace('~0', '~')
        doc = doc[int(part)] if isinstance(doc, list) else doc[part]
    return doc


def target(reference, base):
    path, fragment = urldefrag(reference)
    require('://' not in path, 'Remote reference prohibited: ' + reference)
    resolved = (base.parent / path).resolve() if path else base
    require(resolved.is_relative_to(ROOT), 'Reference outside package: ' + reference)
    return pointer(read(resolved), fragment), resolved


def expand(value, base, seen=()):
    if isinstance(value, dict):
        if set(value) == {'$ref'}:
            token = (str(base), value['$ref'])
            require(token not in seen, 'Cyclic example reference')
            result, new_base = target(value['$ref'], base)
            return expand(result, new_base, seen + (token,))
        return {key: expand(item, base, seen) for key, item in value.items()}
    if isinstance(value, list):
        return [expand(item, base, seen) for item in value]
    return value


def walk(value):
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from walk(item)
    if isinstance(value, list):
        for item in value:
            yield from walk(item)


checker = FormatChecker()


@checker.checks('date-time', raises=(ValueError, TypeError))
def utc_timestamp(value):
    if not isinstance(value, str):
        return True
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z', value):
        return False
    datetime.fromisoformat(value.replace('Z', '+00:00'))
    return True


def main(check_manifest=False, package_only=False):
    schema_files = sorted((ROOT / 'schemas').glob('*.json'))
    registry = Registry()
    for path in schema_files:
        schema = read(path)
        Draft202012Validator.check_schema(schema)
        resource = Resource.from_contents(schema)
        registry = registry.with_resource(path.as_uri(), resource)
        if '$id' in schema:
            registry = registry.with_resource(schema['$id'], resource)
    COUNTS['schema_documents'] = len(schema_files)
    domain = read(ROOT / 'schemas/aist.schema.json')
    COUNTS['definitions'] = len(domain['$defs'])

    def check(value, reference, base, expect_valid=True):
        path, fragment = urldefrag(reference['$ref'])
        full = (base.parent / path).resolve().as_uri() + ('#' + fragment if fragment else '')
        validator = Draft202012Validator({'$ref': full}, registry=registry, format_checker=checker)
        errors = list(validator.iter_errors(value))
        if expect_valid:
            require(not errors, str(base.name) + ' ' + reference['$ref'] + ': ' +
                    '; '.join(error.message[:500] for error in errors[:3]))
        else:
            require(errors, 'Negative example unexpectedly valid: ' + reference['$ref'])

    reference_count = 0
    json_paths = sorted(p for p in ROOT.rglob('*.json')
                        if not any(part.startswith('.') for part in p.relative_to(ROOT).parts)
                        and p.name not in ('CHECKS.json',))
    for path in json_paths:
        data = read(path)
        for node in walk(data):
            if isinstance(node, dict) and isinstance(node.get('$ref'), str):
                target(node['$ref'], path)
                reference_count += 1
    COUNTS['resolved_local_refs'] = reference_count

    http = read(ROOT / 'http.json')
    check(http, {'$ref': 'schemas/http.schema.json'}, ROOT / 'http.json')
    require(set(http['error_statuses']) == set(domain['$defs']['ErrorCode']['enum']),
            'HTTP error mapping differs from ErrorCode')
    require(set(http['common_errors']) <= set(http['error_statuses']), 'Unknown common error')
    routes = set()
    for name, op in http['operations'].items():
        route = (op['method'], op['path'])
        require(route not in routes, 'Duplicate route: ' + name)
        routes.add(route)
        names = set(re.findall(r'{([^}]+)}', op['path']))
        if names:
            schema, _ = target(op['path_parameters']['$ref'], ROOT / 'http.json')
            require(names == set(schema['properties']) == set(schema['required']), 'Path mismatch: ' + name)
        else:
            require(op['path_parameters'] is None, 'Unexpected path parameters')
        require(set(op['errors']) <= set(http['error_statuses']), 'Unknown operation error')
        # Search is a POST read with a bounded JSON scope; cached embeddings do not create a document revision.
        require(op['idempotency'] == (op['method'] == 'POST' and name not in ('searchEvidence', 'login', 'logout')), 'Write missing idempotency')
        if op['idempotency']:
            require(op['headers']['$ref'].endswith('/WriteHeaders'), 'Missing write headers')
            require('idempotency_conflict' in op['errors'], 'Missing conflict response')
    COUNTS['http_operations'] = len(routes)

    registry_doc = read(ROOT / 'registry.json')
    check(registry_doc, {'$ref': 'schemas/aist.schema.json#/$defs/Registry'}, ROOT / 'registry.json')
    tools = read(ROOT / 'tools.json')['tools']
    require(set(tools) == set(domain['$defs']['ToolName']['enum']), 'Tool names differ')
    for name,item in tools.items():
        require(set(item) == {'arguments', 'reply', 'writes', 'requires_llm'}, 'Tool metadata shape')
        require(item['requires_llm'] == (name in ('aist_search_evidence','aist_get_facts','aist_research')),
                'Only semantic search, missing-fact extraction or explicit research may invoke a model')
    for skill in registry_doc['skills']:
        require(set(skill['allowed_tools']) <= set(tools), 'Unknown tool in skill')
        require(skill['status'] != 'enabled' or skill['bundle_sha256'] is not None,
                'Enabled skill without bundle hash')
    for model in registry_doc['model_profiles']:
        require(model['status'] != 'enabled' or model['configuration_sha256'] is not None,
                'Enabled profile without configuration hash')
    COUNTS['tool_operations'] = len(tools)
    COUNTS['skill_registrations'] = len(registry_doc['skills'])

    ep = ROOT / 'examples/entities.json'
    records = read(ep)['records']
    for name, record in records.items():
        try:
            check(record['value'], record['schema'], ep)
        except Exception as exc:
            raise AssertionError('Entity ' + name + ': ' + str(exc)) from exc
    COUNTS['positive_entity_examples'] = len(records)
    np = ROOT / 'examples/schema-negative.json'
    negatives = read(np)['cases']
    for case in negatives:
        check(case['value'], case['schema'], np, expect_valid=False)
    COUNTS['negative_schema_examples'] = len(negatives)

    ap = ROOT / 'SCENARIOS.md'
    assertion_ids = set(re.findall(r'^\| (A[0-9]+) \|', ap.read_text(), re.M))
    hp = ROOT / 'examples/http-chain.json'
    cases = read(hp)['cases']
    require(len({case['id'] for case in cases}) == len(cases), 'Duplicate HTTP case ID')
    success = error = 0
    expanded_cases = {}
    for case in cases:
        case = expand(case, hp)
        expanded_cases[case['id']] = case
        op = http['operations'][case['operation']]
        require(set(case['future_assertions']) <= assertion_ids, 'Missing future assertion')
        for field, schema_field in [('path', 'path_parameters'), ('query', 'query'), ('headers', 'headers')]:
            if op[schema_field]:
                check(case[field], op[schema_field], ROOT / 'http.json')
            else:
                require(not case[field], 'Unexpected ' + field + ': ' + case['id'])
        if op['request']:
            check(case['request'], op['request']['schema'], ROOT / 'http.json')
        else:
            require(case['request'] is None, 'Unexpected body on read')
        status = str(case['response']['status'])
        body = case['response']['body']
        if status in op['responses']:
            response = op['responses'][status]
            if response['schema']:
                check(body, response['schema'], ROOT / 'http.json')
            else:
                require(body is None, 'Binary fixture must not pretend to contain a file')
            if response['headers']:
                check(case['response']['headers'], response['headers'], ROOT / 'http.json')
            success += 1
        else:
            check(body, http['error_schema'], ROOT / 'http.json')
            code = body['error']['code']
            require(code in op['errors'] + http['common_errors'], 'Undeclared operation error: ' + code)
            require(http['error_statuses'][code] == int(status), 'Incorrect error HTTP status')
            error += 1
    COUNTS['http_success_examples'] = success
    COUNTS['http_refusal_examples'] = error
    COUNTS['future_assertions'] = len(assertion_ids)

    # Consistency of authored fixtures only. These checks do not implement API behavior.
    values = [entry['value'] for entry in records.values()]
    facts = {value['fact_id']: value for entry in records.values()
             if entry['schema']['$ref'].endswith('/Fact') for value in [entry['value']]}
    source_versions = {value['id']: value for entry in records.values()
                       if entry['schema']['$ref'].endswith('/SourceVersion') and entry['value']['processing_state'] == 'ready' for value in [entry['value']]}
    calculations = {value['calculation_id']: value for entry in records.values()
                    if entry['schema']['$ref'].endswith('/Calculation') for value in [entry['value']]}
    versions = {value['id']: value for entry in records.values()
                if entry['schema']['$ref'].endswith('/ArtifactVersion') for value in [entry['value']]}
    model_calls = {value['id']: value for entry in records.values()
                   if entry['schema']['$ref'].endswith('/ModelCall') for value in [entry['value']]}
    usage_entries = {value['id']: value for entry in records.values()
                     if entry['schema']['$ref'].endswith('/UsageEntry') for value in [entry['value']]}
    for value in versions.values():
        require(value['content_sha256'] == digest({'payload': value['payload'], 'basis': value['basis']}),
                'Content SHA mismatch: ' + value['id'])
        if value['parent_version_id']:
            parent = versions[value['parent_version_id']]
            require(parent['artifact_id'] == value['artifact_id'], 'Cross-artifact parent')
            require(parent['revision'] < value['revision'], 'Parent revision must precede child')
        for fact in value['basis']['facts']:
            require(fact == facts[fact['fact_id']], 'Changed canonical fact snapshot')
        for node in walk(value):
            if isinstance(node, dict) and set(node) == {'source_version_id', 'fact_id'}:
                require(facts[node['fact_id']]['source']['source_version_id'] == node['source_version_id'],
                        'Unqualified or foreign fixture fact reference')
            if isinstance(node, dict) and 'calculation_ids' in node:
                require(set(node['calculation_ids']) <= set(calculations), 'Unknown fixture calculation')
    for fact in facts.values():
        source = source_versions[fact['source']['source_version_id']]
        require(fact['source']['sha256'] == source['sha256'], 'Fixture source SHA mismatch')
        for evidence in fact['evidence']:
            require(evidence['source'] == fact['source'], 'Evidence source differs')
            require(evidence['locator']['pdf_page'] <= source['page_count'], 'Fixture page out of bounds')
    for calc in calculations.values():
        inputs = {item['name']: facts[item['fact']['fact_id']]['amount'] for item in calc['inputs']}
        require(set(inputs) == {'previous', 'current'}, 'Calculation input names')
        numbers = {key: Fraction(Decimal(value['value'])) * int(value['scale']) for key, value in inputs.items()}
        exact = (numbers['current'] - numbers['previous']) / numbers['previous'] * 100
        require(exact == Fraction(int(calc['exact']['numerator']), int(calc['exact']['denominator'])),
                'Exact calculation mismatch')
        rounded = (Decimal(exact.numerator) / Decimal(exact.denominator)).quantize(
            Decimal(1).scaleb(-calc['rounding']['places']), rounding=ROUND_HALF_UP)
        require(rounded == Decimal(calc['rounded']['value']), 'Rounded calculation mismatch')
    for entry in records.values():
        if entry['schema']['$ref'].endswith('/VersionView'):
            value = entry['value']; version = value['version']; review = value['review']
            require(review['artifact_version_id'] == version['id'], 'Review version mismatch')
            require(set(review['validation_ids']) == {c['id'] for c in value['checks']}, 'Check ID mismatch')
            for item in value['checks']:
                require(item['content_sha256'] == version['content_sha256'] and
                        item['artifact_version_id'] == version['id'], 'Check scope mismatch')
            if review['acceptance']:
                require(review['acceptance']['artifact_version_id'] == version['id'], 'Acceptance inherited')
                require(review['acceptance']['content_sha256'] == version['content_sha256'], 'Acceptance SHA mismatch')
    for call in model_calls.values():
        usage = usage_entries[call['usage_entry_id']]
        require(usage['model_call_id'] == call['id'] and usage['run_id'] == call['run_id'], 'Usage correlation mismatch')
    for entry in records.values():
        if entry['schema']['$ref'].endswith('/Export'):
            value = entry['value']; version = versions[value['artifact_version_id']]
            require(value['content_sha256'] == version['content_sha256'], 'Export content hash mismatch')
            fields = ['project_id','artifact_version_id','content_sha256','format','template_version','renderer_version','settings','acceptance_id']
            require(value['identity_sha256'] == digest({key: value[key] for key in fields}), 'Export identity mismatch')
    require(expanded_cases['05-question']['request'] == expanded_cases['11-repeat']['request'], 'Repeat changed request')
    require(expanded_cases['05-question']['response']['body']['run']['id'] ==
            expanded_cases['11-repeat']['response']['body']['run']['id'], 'Repeat changed run ID')
    require(expanded_cases['15-edit']['response']['body']['version']['id'] ==
            expanded_cases['16-edit-repeat']['response']['body']['version']['id'], 'Edit repeat changed version')
    require(expanded_cases['21-download']['response']['headers'] ==
            expanded_cases['22-download-again']['response']['headers'], 'Download headers changed')
    event_page = records['question_events']['value']
    require([event['seq'] for event in event_page['events']] == list(range(1, 6)), 'Event sequence gap')
    require(records['reconnected_events']['value']['events'] == event_page['events'][3:], 'Reconnect fixture mismatch')
    require(records['note_v2_view']['value']['review']['acceptance'] is None and
            records['note_v3_view']['value']['review']['acceptance'] is None, 'New version inherited acceptance')
    require(records['cancelled']['value']['result_version_id'] is None and
            records['model_late']['value']['delivery'] == 'suppressed', 'Cancelled late response fixture inconsistent')
    COUNTS['fixture_relational_checks'] = 'passed; fixtures only, not API/DB'

    if not package_only:
        inputs = read(ROOT / 'INPUTS.json')
        for item in inputs['files']:
            require(hashlib.sha256(Path(item['path']).read_bytes()).hexdigest() == item['sha256'],
                    'Input changed: ' + item['path'])
        for repo in inputs['repositories']:
            def git(*args):
                return subprocess.check_output(['git','--no-optional-locks','-C',repo['path'],*args], text=True)
            require(git('rev-parse','HEAD').strip() == repo['head'], 'Input HEAD changed')
            require(git('status','--porcelain','--untracked-files=no') == repo['tracked_status'], 'Input tracked status changed')
        COUNTS['unchanged_input_files'] = len(inputs['files'])
        COUNTS['unchanged_repository_heads_and_tracked_statuses'] = len(inputs['repositories'])
    else:
        COUNTS['input_provenance'] = 'not requested in package-only mode'

    for path in ROOT.glob('*.md'):
        require('\u2013' not in path.read_text() and '\u2014' not in path.read_text(), 'Long dash: ' + path.name)
        for url in re.findall(r'\]\(([^)]+)\)', path.read_text()):
            if not re.match(r'[a-z]+://', url):
                target_path = ROOT / url.split('#')[0]
                require(target_path.exists(), 'Broken Markdown link: ' + url)
    if check_manifest:
        manifest = ROOT / 'MANIFEST.sha256'
        listed = set()
        for line in manifest.read_text().splitlines():
            sha, name = line.split('  ', 1)
            require(name not in listed, 'Duplicate manifest entry')
            listed.add(name)
            require(hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == sha, 'Manifest mismatch: ' + name)
        actual = {str(p.relative_to(ROOT)) for p in ROOT.rglob('*') if p.is_file()
                  and not any(part.startswith('.') for part in p.relative_to(ROOT).parts)
                  and p.name != 'MANIFEST.sha256'}
        require(listed == actual, 'Manifest coverage mismatch')
        COUNTS['manifest_entries'] = len(listed)
    else:
        COUNTS['manifest_entries'] = 'not requested; use --manifest after sealing'
    versions = {name: importlib.metadata.version(name) for name in
                ['jsonschema','referencing','attrs','rpds-py','jsonschema-specifications']}
    print(json.dumps({'status':'passed','scope':'offline_contract_and_authored_examples_only',
                      'product_runtime_executed':False,'model_requests':0,'checks':COUNTS,
                      'environment':{'python':platform.python_version(),'executable':sys.executable,
                                     'packages':versions}}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest', action='store_true')
    parser.add_argument('--package-only', action='store_true', help='Skip external evidence and Git checks after copying the contract')
    args = parser.parse_args()
    try:
        main(args.manifest, args.package_only)
    except Exception as exc:
        print(json.dumps({'status':'failed','error':str(exc)},ensure_ascii=False),file=sys.stderr)
        raise SystemExit(1) from exc
