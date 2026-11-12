import React, {useId, useRef, useState} from 'react';

import {useAnchoredMenu} from './useAnchoredMenu';
import {WorkspaceIcon} from './WorkspaceIcon';

export const skillDescription = 'Готовит обзор компании по выбранному материалу, приводит финансовые показатели и указывает, какие сведения требуют сверки.';

export function SkillPicker({disabled, onChoose, onSkills}: {disabled:boolean;onChoose:()=>void;onSkills?:()=>void}) {
  const id = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  const popup = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [details, setDetails] = useState(false);

  function close(focusTrigger = false) {
    setOpen(false);
    setDetails(false);
    if (focusTrigger) trigger.current?.focus();
  }

  useAnchoredMenu(open,trigger,popup,close,true);

  return <>
    <button ref={trigger} className="start-tool" disabled={disabled} aria-expanded={open} aria-controls={open?id:undefined}
      onClick={() => open ? close() : setOpen(true)}>Навыки <WorkspaceIcon name="chevron"/></button>
    {open && <div className="skill-selection" role="group" aria-label="Выбрать навык" id={id} ref={popup}>
      <h3>Выбрать навык</h3>
      <div className="skill-option">
        <button type="button" aria-describedby={id+'-description'} onClick={() => { close(); onChoose(); }}>
          <WorkspaceIcon name="document"/>Аналитическая записка
        </button>
        <button type="button" className="skill-info" aria-label="Описание навыка" aria-expanded={details}
          aria-controls={id+'-description'} onClick={() => setDetails(!details)}>i</button>
      </div>
      {onSkills && <div className="all-skills"><button type="button" onClick={() => {close(); onSkills();}}>
        <WorkspaceIcon name="layers"/>Все навыки</button></div>}
      <p id={id+'-description'} className={'skill-tip'+(details?' expanded':'')}>Собирает справку о компании по выбранному PDF и отмечает сведения для сверки.</p>
    </div>}
  </>;
}
