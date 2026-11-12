import type {VersionView} from './types';
export type Payload=VersionView['version']['payload'];
type Section=NonNullable<Payload['content']['sections']>[number];
type Change={label:string;before:unknown;after:unknown;current:unknown;apply:(p:Payload)=>void;stableBasis:boolean};
const same=(a:unknown,b:unknown)=>JSON.stringify(a)===JSON.stringify(b);

export function pendingChanges(base:Payload,draft:Payload,latest:Payload){
  const changes:Change[]=[];
  function add(label:string,before:unknown,after:unknown,current:unknown,apply:Change['apply'],stableBasis=true){
    if(!same(before,after)&&!same(current,after))changes.push({label,before,after,current,apply,stableBasis});
  }
  const labels={title:'Название записки',audience:'Аудитория',team:'Команда'};
  for(const k of ['title','audience','team'] as const)add(labels[k],base.content[k],draft.content[k],latest.content[k],p=>{p.content[k]=draft.content[k];});
  const sections=(p:Payload)=>p.content.sections??[];
  for(const id of new Set([...sections(base),...sections(draft)].map(s=>s.section_id))){
    const find=(p:Payload)=>sections(p).find(s=>s.section_id===id);
    const a=find(base),b=find(draft),c=find(latest);const label=(b??a)!.title;
    if(!a||!b||!c){
      add(label,a,b,c,p=>{
        const rows=p.content.sections!,i=rows.findIndex(s=>s.section_id===id);
        if(!b){if(i>=0)rows.splice(i,1);}
        else if(i>=0)rows[i]=structuredClone(b);else rows.splice(Math.min(sections(draft).indexOf(b),rows.length),0,structuredClone(b));
      });continue;
    }
    add(label,a.title,b.title,c.title,p=>{find(p)!.title=b.title;});
    for(const bid of new Set([...a.blocks,...b.blocks].map(block=>block.block_id))){
      const block=(s:Section)=>s.blocks.find(v=>v.block_id===bid);
      const old=block(a),updated=block(b),live=block(c);
      if(old?.kind==='table'&&updated?.kind==='table'&&live?.kind==='table'){
        const table=(p:Payload)=>find(p)!.blocks.find(x=>x.block_id===bid&&x.kind==='table') as typeof old;
        add(label+' · подпись',old.caption,updated.caption,live.caption,p=>{table(p).caption=updated.caption;});
        for(const row of updated.rows){
          const before=old.rows.find(r=>r.row_id===row.row_id),cur=live.rows.find(r=>r.row_id===row.row_id);
          if(!before)continue;
          const stableBasis=!!cur&&same(before.fact_refs,cur.fact_refs)&&same(before.calculation_ids,cur.calculation_ids)
            &&same(before.visual_observation_ids,cur.visual_observation_ids);
          for(const k of ['amount','period_label','label'] as const){
            add(row.label+' · '+row.period_label,before[k],row[k],cur?.[k],p=>{
              const target=table(p).rows.find(r=>r.row_id===row.row_id)!;
              if(k==='amount')target.amount=structuredClone(row.amount);else target[k]=row[k];
            },stableBasis);
          }
        }
      }else add(label+' · абзац или блок',old,updated,live,p=>{
        const rows=find(p)!.blocks,i=rows.findIndex(v=>v.block_id===bid);
        if(!updated){if(i>=0)rows.splice(i,1);}
        else if(i>=0)rows[i]=structuredClone(updated);else rows.splice(Math.min(b.blocks.indexOf(updated),rows.length),0,structuredClone(updated));
      });
    }
  }
  return changes.map(c=>({...c,compatible:c.stableBasis&&same(c.before,c.current)}));
}
