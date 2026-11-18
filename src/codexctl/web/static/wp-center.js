const wpFrame = document.getElementById('wp-center-frame');

window.addEventListener('message', async (event) => {
    const port = event.ports[0];
    if (event.origin !== 'null' || event.source !== wpFrame.contentWindow || !port) return;
    const {endpoint, query, body} = event.data ?? {};
    if (!['/plugin', '/config'].includes(endpoint) ||
        typeof query !== 'string' || !query.startsWith('?') || query.length > 512 ||
        typeof body !== 'string' || body.length > 1024 * 1024) {
        port.postMessage({error: 'WP 建站中心请求格式无效'});
        port.close();
        return;
    }
    try {
        const response = await fetch(wpFrame.dataset.proxy + endpoint + query, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/x-www-form-urlencoded',
                'X-CSRFToken': wpFrame.dataset.csrf,
            },
            body,
        });
        port.postMessage({status: response.status, body: await response.text()});
    } catch {
        port.postMessage({error: '无法连接 WP 建站中心代理'});
    } finally {
        port.close();
    }
});

wpFrame.src = wpFrame.dataset.src;
