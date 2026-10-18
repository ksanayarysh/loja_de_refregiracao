// Service worker da admin M.T.F — servido em /sw.js (escopo: o site inteiro).
// Não guarda páginas em cache: a admin sempre mostra dados atuais.

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));

// Notificação recebida do servidor (pedido pago etc.)
self.addEventListener('push', event => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { data = { body: event.data && event.data.text() }; }
  event.waitUntil(self.registration.showNotification(data.title || 'M.T.F Admin', {
    body: data.body || '',
    icon: '/pwa/icon-192.png',
    badge: '/pwa/icon-192.png',
    tag: data.tag || undefined,          // mesma tag = substitui a anterior em vez de empilhar
    renotify: !!data.tag,
    vibrate: [200, 100, 200],
    data: { url: data.url || '/site-orders' },
  }));
});

// Toque na notificação: abre (ou foca) a admin na página do pedido
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const url = new URL(event.notification.data.url, self.location.origin).href;
  event.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const w of wins) {
      if (new URL(w.url).origin === self.location.origin) {
        await w.focus();
        return w.navigate(url);
      }
    }
    return self.clients.openWindow(url);
  })());
});
