const REQUEST_LINE_PATTERN = /^([^ ]+) +([^ ]+) +[^ ]+$/;
const ROOT_TARGET = '/';
const USER_POST_TARGET_PATTERN = /^\/users\/([^/]+)\/posts\/([^/]+)$/;

const buildResponse = (status, { headers = [], body = '' } = {}) =>
  [
    `HTTP/1.1 ${status}`,
    ...headers,
    `Content-Length: ${Buffer.byteLength(body, 'latin1')}`,
    'Connection: close',
    '',
    body,
  ].join('\r\n');

const OK_RESPONSE = buildResponse('200 OK');
const BAD_REQUEST_RESPONSE = buildResponse('400 Bad Request');
const NOT_FOUND_RESPONSE = buildResponse('404 Not Found');
const METHOD_NOT_ALLOWED_RESPONSE = buildResponse('405 Method Not Allowed', {
  headers: ['Allow: GET'],
});

const routeRequestLine = (requestLine) => {
  const requestLineMatch = REQUEST_LINE_PATTERN.exec(requestLine);
  if (!requestLineMatch) return BAD_REQUEST_RESPONSE;

  const [, method, target] = requestLineMatch;
  const userPostMatch = USER_POST_TARGET_PATTERN.exec(target);
  if (target !== ROOT_TARGET && !userPostMatch) return NOT_FOUND_RESPONSE;
  if (method !== 'GET') return METHOD_NOT_ALLOWED_RESPONSE;
  if (!userPostMatch) return OK_RESPONSE;

  const [, userId, postId] = userPostMatch;
  return buildResponse('200 OK', {
    headers: ['Content-Type: text/plain'],
    body: `${userId} ${postId}`,
  });
};

module.exports = { BAD_REQUEST_RESPONSE, routeRequestLine };
