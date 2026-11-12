// Platform credentials stay in memory. A supplied URL fragment is removed immediately.
export function initialToken() {
  const params = new URLSearchParams(location.hash.slice(1));
  const token = params.get('token') || '';
  if (params.has('token')) history.replaceState(null, '', location.pathname);
  return token;
}
