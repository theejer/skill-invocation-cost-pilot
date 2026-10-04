const net = require('net');
const { BAD_REQUEST_RESPONSE, routeRequestLine } = require('./router');

const PORT = process.env.PORT || 8080;
const DEFAULT_MAX_REQUEST_LINE_BYTES = 8192;
const REQUEST_LINE_END = '\r\n';

const readMaxRequestLineBytes = () => {
  const configuredValue = process.env.MAX_REQUEST_LINE_BYTES ?? DEFAULT_MAX_REQUEST_LINE_BYTES;
  const maxRequestLineBytes = Number(configuredValue);
  if (!Number.isSafeInteger(maxRequestLineBytes) || maxRequestLineBytes <= 0) {
    throw new Error(`MAX_REQUEST_LINE_BYTES must be a positive integer, got "${configuredValue}"`);
  }
  return maxRequestLineBytes;
};

const MAX_REQUEST_LINE_BYTES = readMaxRequestLineBytes();

const findRequestLine = (receivedBytes) => {
  const requestLineEnd = receivedBytes
    .subarray(0, MAX_REQUEST_LINE_BYTES)
    .indexOf(REQUEST_LINE_END);
  return requestLineEnd === -1 ? null : receivedBytes.toString('latin1', 0, requestLineEnd);
};

const server = net.createServer((conn) => {
  let receivedBytes = Buffer.alloc(0);

  const sendResponse = (response) => conn.end(response, 'latin1');

  conn.on('error', () => {});
  conn.on('data', (chunk) => {
    if (conn.writableEnded) return;
    receivedBytes = Buffer.concat([receivedBytes, chunk]);

    const requestLine = findRequestLine(receivedBytes);
    if (requestLine !== null) {
      sendResponse(routeRequestLine(requestLine));
    } else if (receivedBytes.length >= MAX_REQUEST_LINE_BYTES) {
      sendResponse(BAD_REQUEST_RESPONSE);
    }
  });
  conn.on('end', () => {
    if (!conn.writableEnded) sendResponse(BAD_REQUEST_RESPONSE);
  });
});

server.listen(PORT);
