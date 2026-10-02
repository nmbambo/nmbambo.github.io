import { readFileSync } from 'node:fs';

export const availability = JSON.parse(readFileSync(new URL('../../assets/book/availability.json', import.meta.url), 'utf8'));

// Monday 5 October 2026, 07:00 SAST
export const MON = new Date('2026-10-05T05:00:00Z');

export const sast = (iso) => new Date(`${iso}+02:00`);

export function makeRequest(over = {}) {
  return {
    v: 1,
    id: '3f2b8c1e-5d4a-4e6f-9a7b-1c2d3e4f5a6b',
    createdAt: '2026-10-02T08:15:00.000Z',
    name: 'Thandi Nkosi',
    email: 'thandi@example.co.za',
    org: 'Acme Holdings',
    reason: 'We keep reversing the same pricing decision. I want to see whether a decision record would stop it.',
    duration: 30,
    platform: 'proton-meet',
    preferred: { start: '2026-10-06T07:00:00.000Z', end: '2026-10-06T07:30:00.000Z' },
    alternative: { start: '2026-10-07T08:00:00.000Z', end: '2026-10-07T08:30:00.000Z' },
    suggestion: '',
    viewerTz: 'Europe/London',
    ...over,
  };
}

export const confirmed = {
  start: '2026-10-07T08:00:00.000Z',
  end: '2026-10-07T08:30:00.000Z',
  platform: 'proton-meet',
  link: 'https://meet.proton.me/join/abc123',
  signalNumber: false,
  uid: '0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d',
};
