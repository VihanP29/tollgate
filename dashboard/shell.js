export const $=id=>document.getElementById(id);
export function el(tag,text,cls) {const n=document.createElement(tag); if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;}
export function button(text,action,cls='secondary') {const n=el('button',text,cls);n.type='button';n.onclick=action;return n;}
export function details(title,value) {const n=el('details');n.append(el('summary',title),el('pre',JSON.stringify(value,null,2)));return n;}
export const money=n=>new Intl.NumberFormat('en-US',{style:'currency',currency:'USD'}).format(n/100);
export function download(value,name) {const u=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json'}));const a=el('a');a.href=u;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(u),1000);}
