import React,{useLayoutEffect,useRef} from 'react';
import type {ParagraphBlock,TextMark} from './types';

// Store plain text and character ranges. Clipboard HTML never becomes note content.
function readParagraph(root:HTMLElement){
  let text='',offset=0;
  const marks:TextMark[]=[];
  const isLine=(node:Node|null)=>node instanceof HTMLElement && (node.tagName==='DIV'||node.tagName==='P');
  function visit(node:Node,bold:boolean,italic:boolean){
    if(node.nodeType===Node.TEXT_NODE){
      const part=node.textContent??'',end=offset+Array.from(part).length;
      if(end>offset&&(bold||italic)){
        const style=bold&&italic?'both':bold?'bold':'italic';
        const last=marks[marks.length-1];
        if(last?.end===offset&&last.style===style)last.end=end;
        else marks.push({start:offset,end,style});
      }
      text+=part;offset=end;return;
    }
    if(!(node instanceof HTMLElement))return;
    if(node.tagName==='BR'){
      // Empty editable lines have a placeholder break. Their block boundary supplies the newline.
      if(!node.previousSibling&&(!node.nextSibling||isLine(node.nextSibling)))return;
      text+='\n';offset++;return;
    }
    for(const child of node.childNodes){
      if(child.previousSibling&&(isLine(child)||isLine(child.previousSibling))){text+='\n';offset++;}
      visit(child,bold||['B','STRONG'].includes(node.tagName),italic||['I','EM'].includes(node.tagName));
    }
  }
  visit(root,false,false);
  return {text,marks};
}

export function EditableParagraph({block,onChange,disabled}:{block:ParagraphBlock;disabled:boolean;onChange:(text:string,marks:TextMark[])=>void}){
  const ref=useRef<HTMLDivElement>(null);
  useLayoutEffect(()=>{
    const root=ref.current;if(!root)return;
    const current=readParagraph(root),marks=block.marks??[];
    if(current.text===block.claim.text&&JSON.stringify(current.marks)===JSON.stringify(marks))return;
    root.replaceChildren();const chars=Array.from(block.claim.text);let pos=0;
    for(const mark of marks){
      root.append(chars.slice(pos,mark.start).join(''));let node:Node=document.createTextNode(chars.slice(mark.start,mark.end).join(''));
      if(mark.style!=='italic'){const b=document.createElement('strong');b.append(node);node=b;}
      if(mark.style!=='bold'){const i=document.createElement('em');i.append(node);node=i;}
      root.append(node);pos=mark.end;
    }
    root.append(chars.slice(pos).join(''));
  },[block]);
  return <div ref={ref} className={'editable-paragraph'+(block.list_level?' note-bullet level-'+block.list_level:'')} contentEditable={!disabled} suppressContentEditableWarning
    role="textbox" aria-label="Абзац" aria-multiline="true" onDrop={e=>e.preventDefault()}
    onKeyDown={e=>{if(e.key==='Enter'){e.preventDefault();document.execCommand('insertText',false,'\n');}}}
    onPaste={e=>{e.preventDefault();document.execCommand('insertText',false,e.clipboardData.getData('text/plain'));}}
    onBeforeInput={e=>{if(e.currentTarget.closest("fieldset:disabled"))e.preventDefault();}} onInput={e=>{const {text,marks}=readParagraph(e.currentTarget);onChange(text,marks);}}/>;
}
