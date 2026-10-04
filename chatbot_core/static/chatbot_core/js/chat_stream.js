/* Consume POST SSE, including frames and UTF-8 characters split across reads. */
async function readChatStream(response, onEvent) {
  if (!response.body) throw new Error('Streaming response has no body');
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      let boundary;
      while ((boundary = /\r?\n\r?\n/.exec(buffer))) {
        const frame = buffer.slice(0, boundary.index);
        buffer = buffer.slice(boundary.index + boundary[0].length);
        const lines = frame.split(/\r?\n/);
        const event = lines.find(line => line.startsWith('event:'))?.slice(6).trim();
        const data = lines.filter(line => line.startsWith('data:'))
          .map(line => line.slice(5).trimStart()).join('\n');
        if (!event || !data) continue; // Heartbeat/comment.
        const payload = JSON.parse(data);
        if (event === 'error') throw new Error(payload.error || 'Reply failed');
        if (['replace', 'delta', 'done'].includes(event)) onEvent(event, payload);
        if (event === 'done') return payload;
      }
      if (done) throw new Error('Reply stream ended before completion');
    }
  } finally {
    try { await reader.cancel(); } finally { reader.releaseLock(); }
  }
}
