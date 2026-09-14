/* Quota Console notification worker.
 *
 * Android Chrome refuses to construct Notification objects from a page
 * ("Failed to construct 'Notification': Illegal constructor. Use
 * ServiceWorkerRegistration.showNotification() instead."), so every
 * notification this plugin raises is rendered through this worker — the one
 * path that behaves the same on mobile and desktop.
 *
 * Clicking a notification focuses an already-open dashboard tab rather than
 * navigating to a new one, mirroring the behaviour the page used to attach to
 * the Notification object itself.
 */

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  event.waitUntil(
    self.clients
      .matchAll({ type: "window", includeUncontrolled: true })
      .then(function (clientList) {
        for (var i = 0; i < clientList.length; i += 1) {
          var client = clientList[i];
          if (client && typeof client.focus === "function") {
            return client.focus();
          }
        }
        if (typeof self.clients.openWindow === "function") {
          return self.clients.openWindow("/quota-console");
        }
        return null;
      })
  );
});
