/* Only the static application shell is cached. Never cache /api or downloads. */
const CACHE='big-base-static-v1';
self.addEventListener('install',event=>event.waitUntil(caches.open(CACHE).then(c=>c.addAll(['/','/manifest.webmanifest','/icon.svg']))));
self.addEventListener('activate',event=>event.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k.startsWith('big-base-static-')&&k!==CACHE).map(k=>caches.delete(k))))));
self.addEventListener('fetch',event=>{
 const request=event.request,url=new URL(request.url);
 if(request.method!=='GET'||url.origin!==self.location.origin||url.search||url.pathname.startsWith('/api/'))return;
 const shell=url.pathname==='/'||url.pathname==='/index.html';
 const asset=url.pathname.startsWith('/assets/')||['/icon.svg','/manifest.webmanifest'].includes(url.pathname);
 if(!shell&&!asset)return;
 event.respondWith(fetch(request).then(response=>{
  if(response.ok){const copy=response.clone();event.waitUntil(caches.open(CACHE).then(c=>c.put(request,copy)))}
  return response;
 }).catch(async()=>{const cached=await caches.match(request);return cached||new Response('Sem conexão. Reconecte para carregar o painel.',{status:503,headers:{'Content-Type':'text/plain; charset=utf-8'}})}));
});
