export class Api {
  constructor(token='') { this.token=token; }
  async request(path, body, method) {
    const controller=new AbortController(); const timer=setTimeout(()=>controller.abort(),20000);
    try {
      const response=await fetch(path,{method:method||(body===undefined?'GET':'POST'),signal:controller.signal,
        headers:{Authorization:'Bearer '+this.token,...(body===undefined?{}:{'Content-Type':'application/json'})},
        body:body===undefined?undefined:JSON.stringify(body)});
      const data=await response.json().catch(()=>({}));
      if (!response.ok) {
        const error=new Error(data.error?.message||({401:'Sign in again to reconnect.',404:'Resource unavailable for this identity.',409:'The record changed. Refresh and try again.',429:'Quota reached. Existing work remains inspectable.',503:'A required dependency is unavailable.'}[response.status])||'Request could not be completed.');
        error.status=response.status; throw error;
      }
      return data;
    } catch(error) {
      if(error.name==='AbortError') throw new Error('Request timed out. Refresh to inspect accepted work before retrying.');
      throw error;
    } finally {clearTimeout(timer);}
  }
}
