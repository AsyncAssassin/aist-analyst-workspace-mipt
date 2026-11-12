"""Credential access is confined to the server transport and read-only billing."""
from pathlib import Path
import os
import re
import stat
import http.client
import json
from decimal import Decimal
from .runs import now


def credential(path):
    p=Path(path)
    metadata=p.stat()
    if not p.is_file() or stat.S_IMODE(metadata.st_mode)&0o077 or metadata.st_uid!=os.geteuid():
        raise ValueError('private server credential required')
    value=p.read_text().removesuffix('\n')
    if not re.fullmatch('[A-Za-z0-9_.-]{8,4096}',value):raise ValueError('invalid credential format')
    return value


def balance(path):
    token=credential(path);connection=http.client.HTTPSConnection('api.novita.ai',443,timeout=30)
    try:
        connection.request('GET','/openapi/v1/billing/balance/detail',headers={'Authorization':'Bearer '+token})
        response=connection.getresponse();raw=response.read(100001)
        if token.encode() in raw or len(raw)>100000:raise ValueError('invalid billing response')
        body=json.loads(raw)
        fields={k:body[k] for k in ('availableBalance','cashBalance','creditLimit','pendingCharges','outstandingInvoices') if k in body}
        return {'observed_at':now(),'method':'GET','http_status':response.status,'raw_financial_fields':fields,
                'USD':{k:str(Decimal(v)/10000) for k,v in fields.items()},'provider_billed_usd':None,
                'qualification':'Balance is not a per-call invoice; no automatic retry.'}
    finally:connection.close()
