"""Операторские команды AIST."""
import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json
import secrets
import sys
import uuid
from sqlalchemy import text
from .auth import create_account, change_account, issue_session
from .config import Settings
from .errors import ApiError
from .database import make_engine


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest='command', required=True)
    user = commands.add_parser('create-user')
    user.add_argument('--label', required=True)
    account = commands.add_parser('create-login')
    account.add_argument('--login', required=True)
    account.add_argument('--expires-at', required=True)
    account.add_argument('--password-stdin', action='store_true')
    disable = commands.add_parser('disable-login')
    disable.add_argument('--login', required=True)
    password = commands.add_parser('set-password')
    password.add_argument('--login', required=True)
    password.add_argument('--password-stdin', action='store_true')
    issue = commands.add_parser('issue-session')
    issue.add_argument('--user-id', required=True)
    issue.add_argument('--ttl-seconds', type=int, default=86400)
    revoke = commands.add_parser('revoke-session')
    revoke.add_argument('--session-id', required=True)
    reconcile = commands.add_parser('reconcile-export')
    reconcile.add_argument('--export-id', required=True)
    retry = commands.add_parser('retry-export')
    retry.add_argument('--export-id', required=True)
    retry.add_argument('--failed-revision', required=True, type=int)
    observed = commands.add_parser('recover-observed')
    observed.add_argument('--run', required=True)
    billing = commands.add_parser('billing-call')
    billing.add_argument('--run', required=True)
    billing.add_argument('--call', required=True)
    billing.add_argument('--evidence')
    billing.add_argument('--apply', action='store_true')
    index = commands.add_parser('index-knowledge')
    index.add_argument('--user-id', required=True)
    index.add_argument('--project-id', required=True)
    index.add_argument('--version-id', required=True, action='append')
    register = commands.add_parser('register-material')
    register.add_argument('--user-id', required=True)
    register.add_argument('--project-id', required=True)
    register.add_argument('--version-id', required=True)
    register.add_argument('--corpus', choices=['factual','samples'], required=True)
    register.add_argument('--company')
    register.add_argument('--period')
    register.add_argument('--provenance', required=True)
    args = parser.parse_args()
    engine = None
    try:
        if args.command == 'billing-call':
            from .accounting import Accounting
            from .billing_evidence import read_invoice
            from .runs import Runs
            from .service import Service

            if args.apply and args.evidence is None:
                parser.error('--apply requires --evidence')
            evidence = read_invoice(args.evidence) if args.evidence is not None else None
            settings = Settings(executor_mode='disabled', live_models_enabled=False, policy_path='')
            engine = make_engine(settings)
            # Billing scopes and ledger changes require only PostgreSQL.
            ledger = Accounting(Runs(Service(engine, None), settings))
            result = (ledger.billing_call(args.run, args.call) if evidence is None
                      else ledger.invoice(args.run, args.call, evidence, apply=args.apply))
            print(json.dumps(result))
            return
        engine = make_engine(Settings())
        if args.command in ('index-knowledge','register-material'):
            from .search import index_material, register
            from .service import Service
            from .storage import Storage
            settings=Settings();service=Service(engine,Storage(settings.storage_root))
            if args.command=='register-material':
                result=register(service,args.user_id,args.project_id,args.version_id,args.corpus,args.company,args.period,args.provenance)
            else:
                result=[index_material(service,settings,args.user_id,args.project_id,vid) for vid in args.version_id]
            print(json.dumps(result,ensure_ascii=False));return
        if args.command in ('reconcile-export', 'retry-export', 'recover-observed'):
            from .runs import Runs
            from .service import Service
            from .storage import Storage
            settings = Settings()
            if args.command == 'recover-observed':
                settings = replace(settings, executor_mode='disabled', live_models_enabled=False, policy_path='')
            runs = Runs(Service(engine, Storage(settings.storage_root)), settings)
            if args.command == 'recover-observed':
                from .received_response import RUN_ID, recover_observed
                if not RUN_ID.fullmatch(args.run):
                    parser.error('--run must be a Run identifier')
                result = recover_observed(runs, args.run)
                print(json.dumps(result))
                if (result.get('status') == 'temporarily_unavailable'
                        or any(call['status'] == 'temporarily_unavailable' for call in result['calls'])):
                    raise SystemExit(1)
            else:
                from .exports import Exports
                exports = Exports(runs)
                if args.command == 'retry-export':
                    if args.failed_revision < 0:
                        parser.error('--failed-revision must be non-negative')
                    result = exports.retry(args.export_id, args.failed_revision)
                else:
                    result = exports.reconcile(args.export_id)
                print(json.dumps(result, ensure_ascii=False))
            return
        with engine.begin() as c:
            if args.command in ('create-login', 'set-password', 'disable-login'):
                secret = None
                if args.command != 'disable-login':
                    secret = sys.stdin.read(1025).removesuffix('\n') if args.password_stdin else secrets.token_urlsafe(32)
                if args.command == 'create-login':
                    expires = datetime.fromisoformat(args.expires_at.replace('Z', '+00:00'))
                    if expires.tzinfo is None or expires <= datetime.now(timezone.utc):
                        parser.error('expires-at must be a future timestamp with timezone')
                    uid = create_account(c, args.login, secret, expires)
                    result = {'user_id': uid, 'login': args.login, 'expires_at': expires.isoformat()}
                else:
                    change_account(c, args.login, secret)
                    result = {'login': args.login, 'sessions_revoked': True}
                if secret is not None and not args.password_stdin:
                    result['password'] = secret
            elif args.command == 'create-user':
                if not 1 <= len(args.label) <= 300:
                    parser.error('label must contain 1-300 characters')
                uid = 'usr_' + uuid.uuid4().hex
                c.execute(text('INSERT INTO aist.users(id,label) VALUES (:id,:label)'), {'id':uid,'label':args.label})
                result = {'user_id':uid}
            elif args.command == 'issue-session':
                if not 1 <= args.ttl_seconds <= 2592000:
                    parser.error('ttl must be 1-2592000 seconds')
                result = issue_session(c, args.user_id, args.ttl_seconds)
                result['ttl_seconds'] = args.ttl_seconds
            else:
                result = {'revoked':c.execute(text('''UPDATE aist.sessions SET revoked_at=clock_timestamp()
                    WHERE id=:id AND revoked_at IS NULL'''), {'id':args.session_id}).rowcount == 1}
        # Сгенерированные секреты выдаются только в приватный stdout оператора.
        print(json.dumps(result))
    except ApiError as error:
        parser.exit(1,error.body['error']['message']+'\n')
    except Exception:
        parser.exit(1, 'Administrative operation failed. Check local database readiness.\n')
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == '__main__':
    main()
