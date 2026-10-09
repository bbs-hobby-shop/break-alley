// BreakAlley service worker — Web Push notifications (Pro feature)
self.addEventListener('push', function(event) {
  var data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) {}
  var title = data.title || 'BreakAlley';
  var options = {
    body: data.body || '',
    icon: '/static/logo-mark.webp',
    badge: '/static/logo-mark.webp',
    data: { url: data.url || '/' },
    tag: data.tag || 'breakalley-push'
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', function(event) {
  event.notification.close();
  var url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function(list) {
      for (var i = 0; i < list.length; i++) {
        var c = list[i];
        if (c.url.indexOf(self.location.origin) === 0) { return c.focus(); }
      }
      return clients.openWindow(url);
    })
  );
});
