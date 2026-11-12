"""Compact model views; stored facts and public tool replies remain unchanged."""
from .facts import source_excerpt_text, numeric_table_passage


def fact_context(facts):
    if any('pdf_page' not in item['locator'] for fact in facts for item in fact['evidence']):
        return dict(facts=facts)
    sources, evidence, passages, limitations = {}, {}, {}, {}
    quotes = []
    for fact in facts:
        for item in fact['evidence']:
            key = (item['source']['source_version_id'], item['locator']['pdf_page'])
            quote = item['quote']
            if (key, quote) not in quotes:
                quotes.append((key, quote))
    # Contained quotations share the full enclosing passage, never a shortened quote.
    longest = [(key, quote) for key, quote in quotes
               if not any(key == other and quote != text and quote in text for other, text in quotes)]
    for key, quote in longest:
        passages['q' + str(len(passages) + 1)] = dict(source_version_id=key[0], page=key[1], quote=quote)
    result = []
    for fact in facts:
        source = fact['source']
        sources[source['source_version_id']] = source
        view = {key: fact[key] for key in ('fact_id', 'metric', 'subject', 'reporting_basis', 'nature', 'period', 'amount')}
        view['source_version_id'] = source['source_version_id']
        if not any(fact['raw_text'] in item['quote'] for item in fact['evidence']):
            view['raw_text'] = fact['raw_text']
        view['evidence_ids'] = []
        for item in fact['evidence']:
            key = (item['source']['source_version_id'], item['locator']['pdf_page'])
            passage = next(pid for pid, entry in passages.items()
                           if (entry['source_version_id'], entry['page']) == key and item['quote'] in entry['quote'])
            eid = item['evidence_id']
            # The same locator ID can occur in different selected documents.
            qualified = key[0] + ':' + eid
            evidence[qualified] = dict(evidence_id=eid, passage=passage, area=item['locator']['area'])
            if item['quote'] != passages[passage]['quote']:
                start = passages[passage]['quote'].index(item['quote'])
                evidence[qualified]['quote_span'] = [start, start + len(item['quote'])]
            view['evidence_ids'].append(qualified)
        if source_excerpt_text(fact) is not None:
            view['excerpt_evidence_id'] = view['evidence_ids'][0]
        elif fact['amount'] is None and numeric_table_passage(fact['raw_text']):
            view['evidence_form'] = 'numeric_table'
        view['limitation_ids'] = []
        for text in fact['limitations']:
            lid = next((key for key, value in limitations.items() if value == text), None)
            if lid is None:
                lid = 'l' + str(len(limitations) + 1)
                limitations[lid] = text
            view['limitation_ids'].append(lid)
        result.append(view)
    return dict(facts=result, fact_sources=sources, evidence=evidence, passages=passages, limitations=limitations)


def model_view(value):
    """Apply only to authorized data at the model boundary, including a saved basis."""
    if isinstance(value, list):
        return [model_view(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: model_view(item) for key, item in value.items() if key != 'facts'}
    if 'facts' in value:
        facts = value['facts']
        if facts and all(isinstance(f, dict) and 'evidence' in f and 'fact_id' in f for f in facts):
            result.update(fact_context(facts))
        else:
            result['facts'] = facts
    return result


def note_reference_labels(pin, facts, extractions):
    """Short, exact references for one prepared note. Only delivered identifiers receive a label."""
    labels = {pin['source_version_id']: 's_1'}
    fact_ids = sorted({fact['fact_id'] for fact in facts})
    visual_ids = sorted({item['observation_id'] for entry in extractions for item in entry.get('visual_observations', [])})
    labels.update({fid: 'f_' + str(i) for i, fid in enumerate(fact_ids, 1)})
    labels.update({vid: 'v_' + str(i) for i, vid in enumerate(visual_ids, 1)})
    return labels


def translate_references(value, mapping, field=None):
    """Translate structured ID fields only. Text, evidence and signed provider messages stay literal."""
    if not mapping:
        return value
    if isinstance(value, dict):
        return {key: translate_references(item, mapping, key) for key, item in value.items()}
    if isinstance(value, list):
        return [translate_references(item, mapping, field) for item in value]
    if isinstance(value, str) and field in ('fact_id', 'source_version_id', 'source_version_ids',
                                            'observation_id', 'visual_observation_ids'):
        return mapping.get(value, value)
    return value
