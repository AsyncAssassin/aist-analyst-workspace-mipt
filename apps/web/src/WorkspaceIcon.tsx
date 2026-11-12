import React from 'react';

export function WorkspaceIcon({name}: {name: 'document'|'layers'|'chevron'|'folder'|'plus'|'pin'|'search'|'chat'|'more'|'close'|'download'|'history'|'menu'}) {
  const paths = {
    document: 'M14 2H5v20h14V7l-5-5Zm0 0v6h5M8 12h8M8 16h8',
    layers: 'm12 3 10 5-10 5L2 8l10-5ZM2 12l10 5 10-5M2 16l10 5 10-5',
    chevron: 'm6 9 6 6 6-6',
    folder: 'M3 5h6l2 2h10v12H3V5Z',
    pin: 'm16 3 5 5-1.5 1.5-2-.5-4 4 .5 2-2 2-5-5 2-2 2 .5 4-4-.5-2L16 3ZM9 15l-6 6',
    search:'M16.5 16.5 21 21M18 10a7 7 0 1 1-14 0 7 7 0 0 1 14 0Z',
    chat:'M3 4h18v13H9l-6 4V4Z',
    more:'M5 12h.8m5.7 0h.8m5.7 0h.8',
    close:'m6 6 12 12M18 6 6 18',
    download:'M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5',
    history:'M3 12a9 9 0 1 1 2.6 6.4M3 6v6h6m3-5v5l3 2',
    menu:'M3 5h18M3 12h18M3 19h18',
    plus: 'M12 5v14M5 12h14',
  };
  return <svg aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="none"
    stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d={paths[name]}/></svg>;
}
