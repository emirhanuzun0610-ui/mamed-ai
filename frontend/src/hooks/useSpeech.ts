import { useState, useCallback, useRef, useEffect } from 'react';
import { transcribeAudio, fetchSpeechHealth } from '../lib/api';

export type SpeechState = 'idle' | 'recording' | 'transcribing';

type SpeechRecognitionCtor = new () => {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  maxAlternatives: number;
  onresult: ((event: { results: ArrayLike<ArrayLike<{ transcript: string }>> }) => void) | null;
  onerror: ((event: { error: string }) => void) | null;
  onend: (() => void) | null;
  start: () => void;
  stop: () => void;
};

function getSpeechRecognitionCtor(): SpeechRecognitionCtor | null {
  if (typeof window === 'undefined') return null;
  const w = window as unknown as {
    SpeechRecognition?: SpeechRecognitionCtor;
    webkitSpeechRecognition?: SpeechRecognitionCtor;
  };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

export function useSpeech() {
  const [state, setState] = useState<SpeechState>('idle');
  const [error, setError] = useState<string | null>(null);
  const [available, setAvailable] = useState(Boolean(getSpeechRecognitionCtor()));
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const streamRef = useRef<MediaStream | null>(null);
  const recognitionRef = useRef<InstanceType<SpeechRecognitionCtor> | null>(null);
  const transcriptRef = useRef<string>('');
  const resolveBrowserRef = useRef<((text: string) => void) | null>(null);

  useEffect(() => {
    const hasBrowserStt = Boolean(getSpeechRecognitionCtor());
    fetchSpeechHealth()
      .then((health) => setAvailable(health.available || hasBrowserStt))
      .catch(() => setAvailable(hasBrowserStt));
  }, []);

  const startRecording = useCallback(async (): Promise<void> => {
    setError(null);
    transcriptRef.current = '';

    const SR = getSpeechRecognitionCtor();
    if (SR) {
      try {
        const rec = new SR();
        rec.lang = 'tr-TR';
        rec.continuous = true;
        rec.interimResults = false;
        rec.maxAlternatives = 1;
        rec.onresult = (ev) => {
          const parts: string[] = [];
          for (let i = 0; i < ev.results.length; i++) {
            const item = ev.results[i]?.[0]?.transcript;
            if (item) parts.push(item.trim());
          }
          transcriptRef.current = parts.join(' ').trim();
        };
        rec.onerror = (ev) => {
          if (ev.error === 'not-allowed' || ev.error === 'service-not-allowed') {
            setError('Mikrofon izni reddedildi');
          }
          setState('idle');
        };
        rec.onend = () => {
          if (resolveBrowserRef.current) {
            const cb = resolveBrowserRef.current;
            resolveBrowserRef.current = null;
            setState('idle');
            cb(transcriptRef.current);
          }
        };
        recognitionRef.current = rec;
        rec.start();
        setState('recording');
        return;
      } catch {
        // Tarayıcı tanıma başlatılamazsa sunucu Whisper yoluna düş
      }
    }

    if (!navigator.mediaDevices?.getUserMedia) {
      setError('Bu tarayıcı mikrofonu desteklemiyor');
      return;
    }

    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;

      const recorder = new MediaRecorder(stream);
      chunksRef.current = [];

      recorder.ondataavailable = (e) => {
        if (e.data.size > 0) chunksRef.current.push(e.data);
      };

      recorder.start();
      mediaRecorderRef.current = recorder;
      setState('recording');
    } catch {
      setError('Mikrofon izni reddedildi');
      setState('idle');
    }
  }, []);

  const stopRecording = useCallback(async (): Promise<string> => {
    if (recognitionRef.current) {
      const rec = recognitionRef.current;
      recognitionRef.current = null;
      return new Promise((resolve) => {
        resolveBrowserRef.current = resolve;
        setState('transcribing');
        try {
          rec.stop();
        } catch {
          resolveBrowserRef.current = null;
          setState('idle');
          resolve(transcriptRef.current);
        }
        window.setTimeout(() => {
          if (resolveBrowserRef.current) {
            const cb = resolveBrowserRef.current;
            resolveBrowserRef.current = null;
            setState('idle');
            cb(transcriptRef.current);
          }
        }, 1200);
      });
    }

    return new Promise((resolve, reject) => {
      const recorder = mediaRecorderRef.current;
      if (!recorder || recorder.state !== 'recording') {
        reject(new Error('Kayıt aktif değil'));
        return;
      }

      recorder.onstop = async () => {
        setState('transcribing');
        streamRef.current?.getTracks().forEach((track) => track.stop());
        streamRef.current = null;

        const blob = new Blob(chunksRef.current, { type: recorder.mimeType || 'audio/webm' });
        chunksRef.current = [];

        try {
          const result = await transcribeAudio(blob, 'recording.webm', {
            language: 'tr',
            hotwords: 'Mamed, Mamed uyan',
          });
          setState('idle');
          resolve(result.text);
        } catch (err) {
          setState('idle');
          const msg = err instanceof Error ? err.message : 'Ses metne çevrilemedi';
          setError(msg);
          reject(err);
        }
      };

      recorder.stop();
    });
  }, []);

  return {
    state,
    error,
    available,
    startRecording,
    stopRecording,
    isRecording: state === 'recording',
    isTranscribing: state === 'transcribing',
  };
}

/**
 * Türkçe uyandırma kelimesi ("Mamed" / "Mamed uyan") dinleyicisi.
 * Destekleyen tarayıcılarda (Chrome/Edge/Android) yerel SpeechRecognition ile çalışır.
 */
export function useWakeWord(
  enabled: boolean,
  onCommand: (commandText: string) => void,
): { listening: boolean } {
  const [listening, setListening] = useState(false);
  const onCommandRef = useRef(onCommand);
  onCommandRef.current = onCommand;

  useEffect(() => {
    if (!enabled) {
      setListening(false);
      return;
    }
    const SR = getSpeechRecognitionCtor();
    if (!SR) {
      setListening(false);
      return;
    }

    let stopped = false;
    let rec: InstanceType<SpeechRecognitionCtor> | null = null;

    const startLoop = () => {
      if (stopped) return;
      try {
        rec = new SR();
        rec.lang = 'tr-TR';
        rec.continuous = true;
        rec.interimResults = false;
        rec.maxAlternatives = 1;
        rec.onresult = (ev) => {
          const last = ev.results[ev.results.length - 1]?.[0]?.transcript?.trim() ?? '';
          if (!last) return;
          const lower = last.toLocaleLowerCase('tr-TR');
          const wakeRegex = /(?:mamed\s+uyan|mamed|memed\s+uyan|memed)[,.:!\s]*(.*)/i;
          const match = lower.match(wakeRegex);
          if (match) {
            const after = (match[1] ?? '').trim();
            const cmd = after || 'Merhaba Mamed, buradayım.';
            onCommandRef.current(cmd);
          }
        };
        rec.onerror = () => {
          setListening(false);
        };
        rec.onend = () => {
          setListening(false);
          if (!stopped) {
            window.setTimeout(startLoop, 600);
          }
        };
        rec.start();
        setListening(true);
      } catch {
        setListening(false);
      }
    };

    startLoop();
    return () => {
      stopped = true;
      setListening(false);
      try {
        rec?.stop();
      } catch {}
    };
  }, [enabled]);

  return { listening };
}
