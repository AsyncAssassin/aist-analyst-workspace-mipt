"""A new pilot user must share the prior Novita budget, including unknown calls."""
from dataclasses import replace
from decimal import Decimal
import json
import os

import pytest
from sqlalchemy import text

from app.accounting import Accounting
from app.embeddings import Embeddings, PRICING
from app.errors import ApiError
from app.runs import Runs
from app.service import ident
from test_d15_postgres import provider, start_run, upload, workspace

pytestmark = pytest.mark.skipif(os.getenv('AIST_TEST_POSTGRES') != 'true', reason='Requires isolated PostgreSQL')


@pytest.mark.parametrize('prior_state', ['observed', 'unknown'])
def test_new_owner_cannot_reset_total_budget(workspace, provider, prior_state, tmp_path):
    # The shared test policy no longer carries the historical ceiling (DRAFT-01). This protection pins a ceiling
    # that the prior owner's exposure alone fills: whatever the database already holds, less than one reservation stays.
    with workspace.engine.connect() as connection:
        held = connection.execute(text('''SELECT coalesce((SELECT sum(exposure) FROM aist.embedding_calls),0)+
            coalesce((SELECT sum(m.exposure) FROM aist.model_calls m JOIN aist.runs r ON r.id=m.run_id
            WHERE r.configuration->>'executor'='openclaw'),0)''')).scalar_one()
    policy = dict(workspace.runs.policy, total_authorized_usd=str(Decimal(held) + Decimal('6.39') + Decimal('0.005')), historical_exposure_usd='0')
    path = tmp_path / 'policy.json'
    path.write_text(json.dumps(policy))
    workspace.settings = replace(workspace.settings, policy_path=str(path))
    workspace.runs = Runs(workspace.service, workspace.settings)
    version = upload(workspace)['version']
    run = start_run(workspace, version)
    embeddings = Embeddings(workspace.engine, workspace.settings)
    prior_owner = ident('usr')
    with workspace.engine.begin() as connection:
        connection.execute(text('INSERT INTO aist.users(id,label) VALUES (:id,:label)'),
                           dict(id=prior_owner, label='Prior synthetic budget owner'))
        prior_call = embeddings.reserve(connection, prior_owner, ['synthetic-budget-input'], 'document', PRICING)
        connection.execute(text('UPDATE aist.embedding_calls SET state=:state,exposure=:cost WHERE id=:id'),
                           dict(id=prior_call, state=prior_state, cost=Decimal('6.39')))
        before = tuple(connection.execute(text('SELECT (SELECT count(*) FROM aist.model_calls), '
                                               '(SELECT count(*) FROM aist.embedding_calls)')).one())
    try:
        # The prior owner's exposure fills the pinned ceiling: the new owner's reservations exceed it.
        with pytest.raises(ApiError) as model_error:
            Accounting(workspace.runs).reserve(run['id'], run['epoch'])
        assert model_error.value.code == 'budget_exceeded'
        with pytest.raises(ApiError) as embedding_error:
            embeddings.vectors(workspace.actor, ['A new uncached pilot question'], 'query')
        assert embedding_error.value.code == 'budget_exceeded'
        assert provider.sent == []
        with workspace.engine.connect() as connection:
            after = tuple(connection.execute(text('SELECT (SELECT count(*) FROM aist.model_calls), '
                                                  '(SELECT count(*) FROM aist.embedding_calls)')).one())
            prior = connection.execute(text('SELECT state,exposure FROM aist.embedding_calls WHERE id=:id'),
                                       dict(id=prior_call)).one()
        assert after == before
        assert tuple(prior) == (prior_state, Decimal('6.39'))
    finally:
        with workspace.engine.begin() as connection:
            connection.execute(text('DELETE FROM aist.embedding_calls WHERE id=:id'), dict(id=prior_call))
        workspace.runs.write(workspace.actor, workspace.session, workspace.project, run['id'], 'cancelRun',
                             ident('key'), dict(expected_revision=run['revision']))
