self.addEventListener('push', event => {
    let data = {};

    try {
        data = event.data ? event.data.json() : {};
    } catch (e) {
        data = {
            title: '이마트24 재입고 알림',
            body: event.data ? event.data.text() : '재고가 입고되었습니다.'
        };
    }

    const title = data.title || '이마트24 재입고 알림';

    const options = {
        body: data.body || '즐겨찾기 상품의 재고가 입고되었습니다.',
        icon: '/static/icon-192.png',
        badge: '/static/icon-192.png',
        tag: data.tag || 'emart24-restock',
        renotify: true,
        data: {
            url: data.url || '/'
        }
    };

    event.waitUntil(
        self.registration.showNotification(title, options)
    );
});

self.addEventListener('notificationclick', event => {
    event.notification.close();

    const target = new URL(
        event.notification.data?.url || '/',
        self.location.origin
    );

    if (target.origin !== self.location.origin) return;

    event.waitUntil(
        clients.matchAll({type: 'window', includeUncontrolled: true})
            .then(async windows => {
                for (const client of windows) {
                    if (new URL(client.url).origin === target.origin) {
                        await client.focus();
                        return client.navigate(target.href);
                    }
                }

                return clients.openWindow(target.href);
            })
    );
});
