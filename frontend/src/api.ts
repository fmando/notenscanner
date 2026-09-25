import axios from 'axios';
import type { ScoreRead, ScoreDetail } from './types';

const api = axios.create({ baseURL: '' });

// Attach JWT token to every request if available
api.interceptors.request.use((config) => {
  const token = localStorage.getItem('auth_token');
  if (token) {
    config.headers = config.headers ?? {};
    config.headers['Authorization'] = `Bearer ${token}`;
  }
  return config;
});

export async function uploadScore(
  files: File | File[],
  ocr: boolean = false
): Promise<ScoreRead> {
  const fileList = Array.isArray(files) ? files : [files];
  const formData = new FormData();
  for (const file of fileList) {
    formData.append('files', file);
  }
  formData.append('ocr', String(ocr));
  const response = await api.post<ScoreRead>('/api/scores', formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
  });
  return response.data;
}

export async function listScores(): Promise<ScoreRead[]> {
  const response = await api.get<ScoreRead[]>('/api/scores');
  return response.data;
}

export async function getScore(id: string): Promise<ScoreDetail> {
  const response = await api.get<ScoreDetail>(`/api/scores/${id}`);
  return response.data;
}

export async function renameScore(id: string, displayName: string): Promise<ScoreRead> {
  const response = await api.patch<ScoreRead>(`/api/scores/${id}`, { display_name: displayName });
  return response.data;
}

export async function deleteScore(id: string): Promise<void> {
  await api.delete(`/api/scores/${id}`);
}

export function scoreStatusUrl(id: string): string {
  const token = localStorage.getItem('auth_token');
  const base = `/api/scores/${id}/status`;
  return token ? `${base}?token=${encodeURIComponent(token)}` : base;
}

export function musicxmlUrl(id: string): string {
  return `/api/scores/${id}/musicxml`;
}

export function pdfUrl(id: string): string {
  return `/api/scores/${id}/pdf`;
}

export function midiUrl(id: string): string {
  return `/api/scores/${id}/midi`;
}

export function partMidiUrl(id: string, partName: string): string {
  return `/api/scores/${id}/parts/${encodeURIComponent(partName)}/midi`;
}

export function exportUrl(id: string, fmt: string): string {
  return `/api/scores/${id}/export/${fmt}`;
}

export function svgInfoUrl(id: string): string {
  return `/api/scores/${id}/svg`;
}

export function svgPageUrl(id: string, page: number): string {
  return `/api/scores/${id}/svg/${page}`;
}

export function originalUrl(id: string): string {
  return `/api/scores/${id}/original`;
}

export function originalInfoUrl(id: string): string {
  return `/api/scores/${id}/original/info`;
}

export function originalPageUrl(id: string, page: number): string {
  return `/api/scores/${id}/original/page/${page}`;
}

// --- Auth API ---

export async function createChallenge(): Promise<{
  challengeId: string;
  qrDataUrl: string;
  loginUri: string;
  mobileUri: string;
}> {
  const response = await axios.post('/auth/challenge');
  return response.data;
}

export async function pollChallenge(challengeId: string): Promise<{
  status: string;
  token?: string;
  coreId?: string;
}> {
  const response = await axios.get(`/auth/challenge/${challengeId}`);
  return response.data;
}

export async function getSession(): Promise<{ authenticated: boolean; coreId?: string }> {
  try {
    const token = localStorage.getItem('auth_token');
    if (!token) return { authenticated: false };
    const response = await axios.get('/auth/session', {
      headers: { Authorization: `Bearer ${token}` },
    });
    return response.data;
  } catch {
    return { authenticated: false };
  }
}

export async function logout(): Promise<void> {
  localStorage.removeItem('auth_token');
  localStorage.removeItem('auth_core_id');
  await axios.post('/auth/logout').catch(() => {});
}

export async function getProfile(): Promise<{ user_name: string }> {
  const response = await api.get<{ user_name: string }>('/auth/profile');
  return response.data;
}

export async function updateProfile(userName: string): Promise<{ user_name: string }> {
  const response = await api.put<{ user_name: string }>('/auth/profile', { user_name: userName });
  return response.data;
}

export default api;
