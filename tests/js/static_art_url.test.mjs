// GitHub #40: stored ``/api/art/{id}`` cover URLs are asked for at the
// ``/api/art/{id}/{size}.jpg`` path (same answer, a static-file path for
// reverse-proxy security tools).  Anything else passes through unchanged.
import test from 'node:test';
import assert from 'node:assert/strict';
import { staticArtUrl } from '../../soniqboom/frontend/js/utils.js';

test('a stored /api/art/{id} becomes the sm .jpg path', () => {
  assert.equal(staticArtUrl('/api/art/abc-123'), '/api/art/abc-123/sm.jpg');
});

test('size and other query parameters are kept', () => {
  assert.equal(staticArtUrl('/api/art/abc?size=lg'), '/api/art/abc/lg.jpg');
  assert.equal(staticArtUrl('/api/art/abc?size=sm&fallback=none&_t=5'),
               '/api/art/abc/sm.jpg?fallback=none&_t=5');
  assert.equal(staticArtUrl('http://h:8080/api/art/abc?size=lg'), 'http://h:8080/api/art/abc/lg.jpg');
});

test('everything else is left alone', () => {
  for (const u of ['', null, undefined, 'data:image/jpeg;base64,AAAA',
                   '/api/art/abc/sm.jpg', '/api/stations/nowplaying-art/x',
                   'https://example.org/logo.png', '/api/artist/info']) {
    assert.equal(staticArtUrl(u), u);
  }
});
