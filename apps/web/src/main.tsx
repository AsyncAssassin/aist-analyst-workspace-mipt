import {AistBrand} from './AistBrand';
import '@fontsource/inter/cyrillic-400.css';
import '@fontsource/inter/latin-400.css';
import '@fontsource/manrope/cyrillic-600.css';
import '@fontsource/manrope/latin-600.css';
import '@fontsource/manrope/cyrillic-700.css';
import '@fontsource/manrope/latin-700.css';
import '@fontsource/jetbrains-mono/cyrillic-400.css';
import '@fontsource/jetbrains-mono/latin-400.css';
import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { ApiError, Client, errorText, loginWithPassword } from './client';
import { clearSession, readLocal, writeLocal, restoreSession, expireSession, confirmNavigation, hasUnsavedEditor } from './local';
import { ChatWorkspace } from './ChatWorkspace';
import { Workspace, QA } from './Workspace';
import './styles.css';

type Identity = {owner_id:string;enabled:boolean;personal_id:string|null};
function Cabinet({token,onLogout}:{token:string;onLogout:(expired?:boolean)=>void}){
  const [client]=useState(()=>new Client(token,undefined,()=>onLogout(true)));
  const [identity,setIdentity]=useState<Identity|null>(null);
  const [error,setError]=useState(''),[attempt,setAttempt]=useState(0);
  useEffect(()=>()=>client.close(),[client]);
  useEffect(()=>{
    const c=client.child();
    void c.json<Identity>('/v1/workspace','WorkspaceIdentity').then(async info=>{
      const previous=readLocal('owner','');
      if(!previous && readLocal('project','')){
        // A legacy draft has no owner marker. Check its original project before migrating the tab.
        const projects=await c.list<{id:string}>('/v1/projects','ProjectPage');
        restoreSession(token,projects.map(p=>p.id));
      } else if(previous!==info.owner_id){clearSession();}
      writeLocal('owner',info.owner_id);writeLocal('session',token);setIdentity(info);setError('');
    }).catch(e=>{if(!c.signal.aborted)setError(errorText(e));});
    return()=>c.close();
  },[client,attempt,token]);
  if(!identity)return <p role="status">{error||'Открываем рабочее место…'} {error&&<button onClick={()=>setAttempt(n=>n+1)}>Обновить</button>}</p>;
  return identity.enabled?<ChatWorkspace client={client} onLogout={()=>onLogout()}/>:<Workspace token={token} personalId={identity.personal_id} onLogout={onLogout}/>;
}
function App() {
  const [token, setToken] = useState(() => readLocal('session', ''));
  const [message, setMessage] = useState('');
  const [expired, setExpired] = useState(false);
  async function logout(invalid = false) {
    if (invalid) {
      expireSession(); setExpired(true);
      setMessage('Сессия истекла или была отозвана. Правки сохранены в этой вкладке. Войдите снова в ту же учетную запись.');
      if (!hasUnsavedEditor()) setToken('');
      return;
    }
    if (!expired) {
      if (!confirmNavigation()) return;
      const client = new Client(token);
      try { await client.json('/v1/auth/logout', 'LogoutResponse', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }); }
      catch (error) { if (!(error instanceof ApiError && error.status === 401)) { setMessage('Не удалось завершить сессию на сервере. Повторите выход.'); return; } }
      finally { client.close(); }
      clearSession(); const url=new URL(location.href);url.searchParams.delete('chat');url.hash='';history.replaceState(null,'',url);
      setMessage('Вы вышли из аккаунта.');
    }
    setToken('');
  }
  return <><a className="skip-link" href="#conversation">К рабочей области</a><header className="topbar"><AistBrand/><span className="build-label">Рабочее место аналитика </span>{token && <button className="secondary" onClick={() => logout()}>{expired?'Войти снова':'Выйти'}</button>}</header>
    {token && message && <p role="status" className="notice">{message}</p>}
    {token ? <Cabinet key={token} token={token} onLogout={logout} /> : <Login message={message} onLogin={value => {setToken(value); setExpired(false); setMessage('');}} />}
  </>;
}
function Login({ onLogin, message }: { onLogin: (token: string) => void; message: string }) {
  const [name, setName] = useState(''); const [password, setPassword] = useState('');
  const [error, setError] = useState(''); const [busy, setBusy] = useState(false);
  const pending = useRef<AbortController | null>(null);
  useEffect(() => () => pending.current?.abort(), []);
  async function login() {
    if (busy) return;
    setBusy(true); setError('');
    const controller = new AbortController(); pending.current = controller;
    try {
      const token = await loginWithPassword(name, password, AbortSignal.any([controller.signal, AbortSignal.timeout(25000)]));
      setPassword(''); onLogin(token);
    } catch (e) { if (!controller.signal.aborted) setError(errorText(e)); }
    finally { if (!controller.signal.aborted) setBusy(false); }
  }
  return <main className="login" id="conversation"><section className="login-intro"><h1>Аналитическая записка<br/>по вашему PDF</h1><p>Загрузите документ, обсудите результаты в чате и откройте источники прямо из записки.</p></section><section className="login-card"><h2>Вход в AIST</h2><p className="muted">Используйте логин и пароль из вашей карточки доступа.</p>{message && <p className="notice" role="status">{message}</p>}<form onSubmit={e => {e.preventDefault(); void login();}}><label>Логин<input autoComplete="username" maxLength={64} spellCheck={false} autoCapitalize="none" value={name} onChange={e => setName(e.target.value)} required /></label><label>Пароль<input type="password" autoComplete="current-password" maxLength={256} value={password} onChange={e => setPassword(e.target.value)} required /></label>{error && <p role="alert" className="error">{error}</p>}<button disabled={busy || !name.trim() || !password}>{busy ? 'Входим…' : 'Войти'}</button></form><p className="small muted">Сессия хранится только в этой вкладке до выхода или её закрытия.</p>{QA && <p className="small muted">В тестовой сборке используется симулятор.</p>}</section></main>;
}
createRoot(document.getElementById('root')!).render(<App />);
