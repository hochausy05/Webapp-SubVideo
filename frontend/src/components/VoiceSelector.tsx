import { useEffect, useRef, useState } from 'react';
import { getTtsReadiness, getVoicePreview, setProjectVoice, setProjectSpeed, startTts } from '../api/projects';

export function VoiceSelector({ projectId, voiceId, speed, translationReady, hasUnsavedEdits, onVoiceChange, onSpeedChange }: { projectId: string; voiceId: string; speed: number; translationReady: boolean; hasUnsavedEdits: boolean; onVoiceChange: (voice: string) => void; onSpeedChange: (speed: number) => void }) {
  const [busy, setBusy] = useState(false);
  const [saving, setSaving] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [error, setError] = useState('');
  const [jobMessage, setJobMessage] = useState('');
  const [readiness, setReadiness] = useState<{ready: boolean; message: string | null}>({ready:false,message:'Đang kiểm tra bản dịch…'});
  const audio = useRef<HTMLAudioElement | null>(null);
  const objectUrl = useRef<string | null>(null);
  const controller = useRef<AbortController | null>(null);

  const stop = () => {
    controller.current?.abort(); controller.current = null;
    if (audio.current) { audio.current.pause(); audio.current = null; }
    if (objectUrl.current) { URL.revokeObjectURL(objectUrl.current); objectUrl.current = null; }
    setPlaying(false); setBusy(false);
  };
  useEffect(() => {
    const stopOtherAudio = () => { controller.current?.abort(); controller.current = null; audio.current?.pause(); audio.current = null; if (objectUrl.current) URL.revokeObjectURL(objectUrl.current); objectUrl.current = null; setPlaying(false); setBusy(false); };
    window.addEventListener('autodub-stop-audio',stopOtherAudio);
    return () => { window.removeEventListener('autodub-stop-audio',stopOtherAudio); controller.current?.abort(); audio.current?.pause(); if (objectUrl.current) URL.revokeObjectURL(objectUrl.current); };
  }, []);
  useEffect(() => {
    let live = true;
    const check = () => { void getTtsReadiness(projectId).then((result) => { if (live) setReadiness(result); }).catch(() => { if (live) setReadiness({ready:false,message:'Không kiểm tra được trạng thái bản dịch. Hãy kiểm tra backend.'}); }); };
    check(); const timer = window.setInterval(check, 4000);
    return () => { live = false; window.clearInterval(timer); };
  }, [projectId, translationReady]);

  const preview = async () => {
    window.dispatchEvent(new Event('autodub-stop-audio')); stop(); setError(''); setBusy(true);
    const request = new AbortController(); controller.current = request;
    try {
      const blob = await getVoicePreview(voiceId, speed, request.signal);
      if (request.signal.aborted) return;
      objectUrl.current = URL.createObjectURL(blob);
      const player = new Audio(objectUrl.current); audio.current = player;
      player.onended = () => { setPlaying(false); };
      await player.play(); if (request.signal.aborted) player.pause(); else setPlaying(true);
    } catch (reason) {
      if (!request.signal.aborted) setError(reason instanceof Error ? reason.message : 'Không thể phát audio.');
    } finally { if (!request.signal.aborted) setBusy(false); }
  };
  const generate = async () => {
    setError(''); setJobMessage(''); setSaving(true);
    try { const result = await startTts(projectId); setJobMessage(result.reused ? 'Job tạo giọng đọc đang chờ hoặc đang chạy.' : 'Đã tạo Job. Xem tiến độ tại tab TTS & đồng bộ.'); setReadiness({ready:false,message:'Job tạo giọng đọc đang chờ hoặc đang chạy.'}); }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Không tạo được Job.'); }
    finally { setSaving(false); }
  };
  const selectVoice = async (next: string) => {
    window.dispatchEvent(new Event('autodub-stop-audio')); stop(); setError(''); setSaving(true);
    try { const result = await setProjectVoice(projectId, next); onVoiceChange(result.voice_id); }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Không lưu được giọng.'); }
    finally { setSaving(false); }
  };
  const selectSpeed = async (next: number) => {
    window.dispatchEvent(new Event('autodub-stop-audio')); stop(); setError(''); setSaving(true);
    try { const result = await setProjectSpeed(projectId, next); onSpeedChange(result.speed); }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Không lưu được tốc độ.'); }
    finally { setSaving(false); }
  };
  return <section className="voice-selector glass-surface" aria-label="Giọng thuyết minh">
    <div className="eyebrow">GIỌNG THUYẾT MINH</div><p>Supertonic 3 · Tiếng Việt</p>
    <label>Chọn giọng<select value={voiceId} disabled={saving} onChange={(event) => void selectVoice(event.target.value)}>
      <optgroup label="Nam">{[1,2,3,4,5].map((n) => <option key={`M${n}`} value={`M${n}`}>Nam M{n}{n === 1 ? ' — Mặc định' : ''}</option>)}</optgroup>
      <optgroup label="Nữ">{[1,2,3,4,5].map((n) => <option key={`F${n}`} value={`F${n}`}>Nữ F{n}</option>)}</optgroup>
    </select></label>
    <label className="speed-control"><span>Tốc độ đọc <strong>{Number(speed).toFixed(2)}×</strong></span><input aria-label="Tốc độ đọc" type="range" min="0" max="4" step="1" value={[1.05,1.15,1.25,1.35,1.45].indexOf(Number(speed))} disabled={saving} onChange={(event) => void selectSpeed([1.05,1.15,1.25,1.35,1.45][Number(event.target.value)])} /><span className="speed-ticks"><span>1.05</span><span>1.15</span><span>1.25</span><span>1.35</span><span>1.45</span></span></label>
    <div className="voice-actions"><button type="button" className="button button-quiet" disabled={busy || saving} onClick={() => void preview()}>{busy ? 'Đang tải model hoặc tạo mẫu…' : '▶ Nghe thử'}</button><button type="button" className="button button-quiet" disabled={!playing && !busy} onClick={stop}>Dừng</button></div>
    <button type="button" className="button button-primary" disabled={saving || !readiness.ready || hasUnsavedEdits} onClick={() => void generate()}>Tạo giọng đọc</button>
    {hasUnsavedEdits && <p>Hãy lưu bản dịch trước khi tạo giọng đọc.</p>}
    {!hasUnsavedEdits && !readiness.ready && <p role="status">{readiness.message}</p>}
    {jobMessage && <p role="status">{jobMessage}</p>}
    <small>Giọng được lưu theo Project.</small>{error && <p className="transcription-error" role="alert">{error}</p>}
  </section>;
}
