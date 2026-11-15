import { useCallback, useEffect, useRef, useState } from 'react';
import { cancelTts, getTtsPlayback, getTtsState, type TtsState } from '../api/projects';

export function TtsResults({ projectId, voiceId, speed }: { projectId: string; voiceId: string; speed: number }) {
  const [state,setState]=useState<TtsState | null>(null);
  const [error,setError]=useState('');
  const [playing,setPlaying]=useState<string | null>(null);
  const player=useRef<HTMLAudioElement | null>(null);
  const playGeneration=useRef(0);
  const refresh=useCallback(async () => { setState(await getTtsState(projectId)); },[projectId]);
  useEffect(() => { void refresh().catch((reason: unknown) => setError(reason instanceof Error ? reason.message : 'Không tải được audio.')); },[refresh,voiceId,speed]);
  useEffect(() => {
    if (!state?.latest_job || !['queued','running'].includes(state.latest_job.status)) return;
    const timer=window.setInterval(() => { void refresh().catch(() => undefined); },3000);
    return () => window.clearInterval(timer);
  },[state?.latest_job?.status,refresh]);
  useEffect(() => {
    const stop = () => { playGeneration.current += 1; player.current?.pause(); player.current=null; setPlaying(null); };
    window.addEventListener('autodub-stop-audio',stop);
    return () => { window.removeEventListener('autodub-stop-audio',stop); player.current?.pause(); };
  },[]);
  const play=async (id: string, raw = false) => {
    window.dispatchEvent(new Event('autodub-stop-audio')); player.current?.pause(); setPlaying(null); setError('');
    const generation=playGeneration.current;
    try { const result=await getTtsPlayback(projectId,id,raw); if (generation!==playGeneration.current) return; const audio=new Audio(result.playback_url); player.current=audio; audio.onended=() => setPlaying(null); await audio.play(); if (generation===playGeneration.current) setPlaying(id); }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Không phát được audio.'); }
  };
  const current=state?.latest_job && ['queued','running'].includes(state.latest_job.status) ? state.latest_job : null;
  return <section className="tts-results"><div className="transcript-heading"><strong>Giọng đọc</strong><span>Audio theo từng Unit</span></div>
    {current && <div className="transcription-progress" role="status"><span className="spinner" />{current.progress_message || 'Đang chờ Worker TTS'} <button type="button" className="text-link" onClick={() => { void cancelTts(projectId,current.id).then(refresh).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : 'Không hủy được Job.')); }}>Hủy</button></div>}
    {state?.latest_job?.status==='failed' && <p className="transcription-error" role="alert">{state.latest_job.error_message || 'Không tạo được giọng đọc.'}</p>}
    {state?.latest_job?.status==='cancelled' && <p role="status">Đã hủy tạo giọng đọc; kết quả trước đó được giữ.</p>}
    {state?.latest_job?.status==='succeeded' && !state.stale && state.items.length > 0 && state.items.every((item) => item.status === 'ready') && <p role="status">Đã tạo giọng đọc.</p>}
    {current?.status==='queued' && Date.now() - Date.parse(current.created_at) > 60000 && <p className="translation-review">Job đang chờ lâu. Hãy kiểm tra TTS Worker trong cửa sổ run.cmd.</p>}
    {state?.stale && <p className="translation-review">Giọng hoặc bản dịch đã thay đổi. Hãy tạo lại audio cho các đoạn không còn khớp.</p>}
    {error && <p className="transcription-error" role="alert">{error}</p>}
    {!state?.items.length && !current && <p className="muted-copy">Chọn giọng và nhấn Tạo giọng đọc để bắt đầu.</p>}
    {state?.items.map((item) => <article className="translation-unit" key={item.id}><div className="transcript-heading"><strong>Đoạn {item.position+1}</strong><span>{(item.duration_us/1000000).toFixed(2)} giây</span></div><p>{item.final_text}</p>
      {item.stale ? <span className="translation-review">Audio cũ không còn khớp</span> : <button type="button" className="button button-quiet" onClick={() => { if (playing===item.id) { player.current?.pause(); setPlaying(null); } else void play(item.id); }}>{playing===item.id ? 'Dừng' : '▶ Phát'}</button>}
      {!item.stale && item.trimmed_samples > 0 && <><span className="muted-copy"> Bản gốc: {((item.original_samples ?? 0) / item.sample_rate).toFixed(2)} giây</span><button type="button" className="button button-quiet" onClick={() => void play(item.id,true)}>Nghe bản gốc</button></>}
    </article>)}
    <p className="muted-copy">Đồng bộ và render sẽ được bổ sung ở giai đoạn sau.</p>
  </section>;
}
