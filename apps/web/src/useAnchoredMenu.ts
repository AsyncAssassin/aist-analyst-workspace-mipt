import {useEffect,useLayoutEffect,useRef,type RefObject} from 'react';

export function useAnchoredMenu(open:boolean,trigger:RefObject<HTMLElement>,popup:RefObject<HTMLElement>,
  onDismiss:(focusTrigger:boolean)=>void,tip=false) {
  const dismiss=useRef(onDismiss);
  dismiss.current=onDismiss;
  useLayoutEffect(() => {
    if (!open) return;
    const menu = popup.current!, anchor = trigger.current!;
    function position() {
      const rect = anchor.getBoundingClientRect();
      const left = Math.max(8, Math.min(rect.left, innerWidth - menu.offsetWidth - 8));
      const below = rect.bottom + 5;
      const top = below + menu.offsetHeight <= innerHeight - 8 ? below : rect.top - menu.offsetHeight - 5;
      menu.style.left = left + 'px';
      menu.style.top = Math.max(8, Math.min(top, innerHeight - menu.offsetHeight - 8)) + 'px';
      if (tip) menu.dataset.tip = left + menu.offsetWidth + 230 < innerWidth ? 'right' : left >= 230 ? 'left' : 'below';
    }
    position();
    const observer = new ResizeObserver(position);
    observer.observe(menu);
    observer.observe(anchor);
    if (anchor.parentElement) observer.observe(anchor.parentElement);
    window.addEventListener('resize', position);
    window.addEventListener('scroll', position, true);
    return () => {
      observer.disconnect();
      window.removeEventListener('resize', position);
      window.removeEventListener('scroll', position, true);
    };
  }, [open,trigger,popup,tip]);

  useEffect(() => {
    if (!open) return;
    function outside(e: Event) {
      const node = e.target as Node;
      if (!popup.current?.contains(node) && !trigger.current?.contains(node)) dismiss.current(false);
    }
    function escape(e: KeyboardEvent) {
      if (e.key === 'Escape') dismiss.current(true);
    }
    document.addEventListener('pointerdown', outside);
    document.addEventListener('focusin', outside);
    document.addEventListener('keydown', escape);
    return () => {
      document.removeEventListener('pointerdown', outside);
      document.removeEventListener('focusin', outside);
      document.removeEventListener('keydown', escape);
    };
  }, [open,trigger,popup]);

}
