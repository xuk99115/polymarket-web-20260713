#!/bin/bash
# rebuild_xray_pool.sh - 重新拉取订阅, 测试节点, 生成 xray config
# Usage: bash rebuild_xray_pool.sh
set -e

SUBS_URL="https://liangxin.xyz/api/v1/liangxin?OwO=6f8a8bac6527d7f8cae11108f83dba70"
TMP_DIR="/root/xray_proxy_setup"
mkdir -p $TMP_DIR
cd $TMP_DIR

echo "=== Step 1: 拉取订阅 ==="
curl -sL -A "Mozilla/5.0" "$SUBS_URL" --max-time 20 -o subs_encoded.txt
# 兼容两种格式: 原始 base64 / 直接 vless:// 行
if head -c 5 subs_encoded.txt | grep -q "vless"; then
    cp subs_encoded.txt subs_decoded.txt
    echo "已直接解码 (新格式)"
else
    cat subs_encoded.txt | tr -d ' \n' | base64 -d > subs_decoded.txt 2>/dev/null
    echo "已 base64 解码"
fi

echo "=== Step 2: 解析 vless 节点 ==="
python3 -c "
import re
with open('subs_decoded.txt') as f:
    lines = [l.strip() for l in f if l.strip()]
vless = []
for l in lines:
    if not l.startswith('vless://'): continue
    m = re.match(r'vless://([^@]+)@([^:]+):(\d+)\?(.+?)(?:#(.+))?\$', l)
    if not m: continue
    uuid, _, fake_port, params, _ = m.groups()
    p = dict(re.findall(r'([^=&]+)=([^&]*)', params))
    vless.append({
        'uuid': uuid,
        'address': p.get('host',''),
        'port': int(fake_port),
        'path': p.get('path','/').replace('%2F','/'),
        'sni': p.get('sni', p.get('host','')),
        'security': p.get('security','none')
    })
vless = [n for n in vless if n['address']]
print(f'找到 {len(vless)} 个 vless 节点')
# 取前 6 个
vless = vless[:6]
import json
json.dump(vless, open('nodes.json','w'), indent=2)
print('Saved to nodes.json')
"

echo "=== Step 3: 生成 xray config ==="
python3 -c "
import json
nodes = json.load(open('nodes.json'))
inbounds, outbounds, rules = [], [], []
for i, n in enumerate(nodes):
    port = 10808 + i * 100
    inbounds.append({
        'tag': f'socks-{i+1}', 'port': port, 'listen': '127.0.0.1',
        'protocol': 'socks',
        'settings': {'auth': 'noauth', 'udp': False},
        'sniffing': {'enabled': True, 'destOverride': ['http','tls']}
    })
    outbounds.append({
        'tag': f'proxy-{i+1}', 'protocol': 'vless',
        'settings': {'vnext': [{'address': n['address'], 'port': n['port'],
            'users': [{'id': n['uuid'], 'encryption': 'none'}]}]},
        'streamSettings': {
            'network': 'ws',
            'security': n['security'] if n['security'] in ('tls','reality') else '',
            'wsSettings': {'path': n['path'], 'headers': {'Host': n['address']}},
            'tlsSettings': {'serverName': n['sni'], 'allowInsecure': False} if n['security']=='tls' else None
        }
    })
    rules.append({'inboundTag': [f'socks-{i+1}'], 'outboundTag': f'proxy-{i+1}', 'type': 'field'})
def clean(o):
    if isinstance(o, dict): return {k: clean(v) for k,v in o.items() if v is not None and v != ''}
    if isinstance(o, list): return [clean(x) for x in o]
    return o
config = {
    'log': {'loglevel': 'warning'},
    'inbounds': [clean(i) for i in inbounds],
    'outbounds': [clean(o) for o in outbounds] + [{'tag': 'direct', 'protocol': 'freedom'}],
    'routing': {'rules': rules}
}
json.dump(config, open('/tmp/xray_multi.json','w'), indent=2)
print(f'生成 config: {len(nodes)} 节点, 端口 10808-11308')
"

echo "=== Step 4: 重启 xray ==="
pkill -9 -f xray_multi 2>/dev/null || true
sleep 1
cd /usr/local/x-ui && nohup ./bin/xray-linux-amd64 -c /tmp/xray_multi.json > /tmp/xray_multi.log 2>&1 & disown
sleep 3

echo "=== Step 5: 测试所有端口 ==="
for p in 10808 10908 11008 11108 11208 11308; do
    code=$(curl -s -x socks5://127.0.0.1:$p https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT --max-time 8 -o /dev/null -w '%{http_code}')
    echo "  port $p: HTTP=$code"
done

echo "=== Step 6: 更新 .env ==="
if [ -f /root/polymarket_web/.env ]; then
    if ! grep -q "BINANCE_PROXY_URLS=" /root/polymarket_web/.env; then
        echo "BINANCE_PROXY_URLS=socks5://127.0.0.1:10808,socks5://127.0.0.1:10908,socks5://127.0.0.1:11008,socks5://127.0.0.1:11108,socks5://127.0.0.1:11208,socks5://127.0.0.1:11308" >> /root/polymarket_web/.env
        echo "已添加 BINANCE_PROXY_URLS 到 .env"
    fi
fi

echo "=== Done ==="