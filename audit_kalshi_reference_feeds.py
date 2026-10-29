"""Read-only audit of Kalshi-hosted reference feeds. Never places orders."""
import base64
import json
import os
import time
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding
import websocket

def auth():
    key_id = os.environ["KALSHI_API_KEY_ID"]
    key = serialization.load_pem_private_key(os.environ["KALSHI_PRIVATE_KEY"].encode(), password=None)
    stamp = str(int(time.time()*1000))
    msg = (stamp+"GET"+"/trade-api/ws/v2").encode()
    if isinstance(key, ed25519.Ed25519PrivateKey):
        sig = key.sign(msg)
    else:
        sig = key.sign(msg,padding.PSS(mgf=padding.MGF1(hashes.SHA256()),salt_length=hashes.SHA256().digest_size),hashes.SHA256())
    return [f"KALSHI-ACCESS-KEY: {key_id}",f"KALSHI-ACCESS-TIMESTAMP: {stamp}",f"KALSHI-ACCESS-SIGNATURE: {base64.b64encode(sig).decode()}"]

def main():
    ws = websocket.create_connection("wss://external-api-ws.kalshi.com/trade-api/ws/v2",header=auth(),timeout=8)
    subs = {}
    for id, channel in [(1,"cfbenchmarks_value"),(2,"pyth_value")]:
        ws.send(json.dumps({"id":id,"cmd":"subscribe","params":{"channels":[channel]}}))
    deadline=time.monotonic()+35
    received=[]
    requested=set()
    while time.monotonic()<deadline:
        try:
            data=json.loads(ws.recv())
        except websocket.WebSocketTimeoutException:
            continue
        typ=data.get("type")
        if typ=="subscribed":
            sid=data.get("msg",{}).get("sid")
            channel=data.get("msg",{}).get("channel")
            subs[channel]=sid
            action="indexlist" if channel=="cfbenchmarks_value" else "underlying_list"
            ws.send(json.dumps({"id":100+len(requested),"cmd":"update_subscription","params":{"sid":sid,"action":action}}))
            requested.add(channel)
        if typ in ("cfbenchmarks_value_index_list","pyth_value_underlying_list","error","ok"):
            received.append(data)
        if typ in ("cfbenchmarks_value","pyth_value") and len(received)<40:
            msg=data.get("msg",{})
            received.append({"type":typ,"index_id":msg.get("index_id"),"underlying_ticker":msg.get("underlying_ticker"),"timestamp":msg.get("source_ts_ms")})
    ws.close()
    print("KALSHI_REFERENCE_AUDIT "+json.dumps({"subscriptions":subs,"messages":received},default=str),flush=True)

if __name__=="__main__":
    main()
