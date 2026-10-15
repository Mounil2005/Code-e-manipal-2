import { useEffect, useRef } from "react";
import { useSim } from "../store/sim";
import { wsUrl } from "../lib/api";

/** Live connection to the authoritative backend. Reconnects with backoff and asks
 * for a full resync whenever a message sequence gap is detected. */
export function useSimSocket(simId: string | undefined) {
  const applySocket = useSim((s) => s.applySocket);
  const setConn = useSim((s) => s.setConn);
  const retry = useRef(0);

  useEffect(() => {
    if (!simId) return;
    let ws: WebSocket | null = null;
    let closed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let ping: ReturnType<typeof setInterval> | undefined;

    const connect = () => {
      setConn(retry.current === 0 ? "connecting" : "reconnecting");
      ws = new WebSocket(wsUrl(`/ws/simulations/${simId}`));
      ws.onopen = () => {
        retry.current = 0;
        setConn("live");
        ping = setInterval(() => ws?.readyState === 1 && ws.send(JSON.stringify({ type: "ping" })), 15000);
      };
      ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data);
        if (msg.type === "pong") return;
        const last = useSim.getState().lastSeq;
        if (!msg.full && msg.seq && last && msg.seq > last + 1) {
          ws?.send(JSON.stringify({ type: "resync" }));
        }
        applySocket(msg);
      };
      ws.onclose = () => {
        clearInterval(ping);
        if (closed) return;
        setConn("reconnecting");
        retry.current += 1;
        timer = setTimeout(connect, Math.min(8000, 400 * 2 ** retry.current));
      };
      ws.onerror = () => ws?.close();
    };
    connect();
    return () => {
      closed = true;
      clearTimeout(timer);
      clearInterval(ping);
      ws?.close();
    };
  }, [simId, applySocket, setConn]);
}
