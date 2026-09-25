import { useState } from 'react';
import 'html-midi-player';
import type { PartRead } from '../types';
import { midiUrl, partMidiUrl } from '../api';

// Explicit baseURL for Magenta's SoundFont/Instrument classes (@magenta/music
// core/soundfont.js) - they fetch "{baseURL}/soundfont.json", then per used
// instrument "{baseURL}/{instrument}/instrument.json" and
// "{baseURL}/{instrument}/p{pitch}_v{velocity}.mp3". The bucket itself is
// fine (verified reachable); the bug was leaving the HTML attribute as an
// empty string, which XHTML treats as "attribute present, value empty" -
// baseURL="" - so every fetch went to OUR OWN origin's root  instead of
// here, 404ing silently (audio stayed mute but playback state still ran).
const SOUND_FONT_URL = 'https://storage.googleapis.com/magentadata/js/soundfonts/sgm_plus';

interface PartPlayerProps {
  scoreId: string;
  parts: PartRead[];
}

export default function PartPlayer({ scoreId, parts }: PartPlayerProps) {
  const [selectedPart, setSelectedPart] = useState<string | null>(null);

  if (parts.length === 0) {
    return <div style={{ padding: '16px', color: '#666' }}>No MIDI available</div>;
  }

  const currentMidiUrl =
    selectedPart === null ? midiUrl(scoreId) : partMidiUrl(scoreId, selectedPart);

  return (
    <div style={{ padding: '16px' }}>
      <div style={{ marginBottom: '12px' }}>
        <label htmlFor="part-select" style={{ marginRight: '8px', fontWeight: 'bold' }}>
          Part:
        </label>
        <select
          id="part-select"
          value={selectedPart ?? ''}
          onChange={(e) => setSelectedPart(e.target.value === '' ? null : e.target.value)}
        >
          <option value="">Full score</option>
          {parts.map((part) => (
            <option key={part.id} value={part.name}>
              {part.name}
            </option>
          ))}
        </select>
      </div>

      <midi-player src={currentMidiUrl} sound-font={SOUND_FONT_URL} />

      <div style={{ marginTop: '12px' }}>
        <a
          href={currentMidiUrl}
          download
          style={{ color: '#0066cc', textDecoration: 'underline' }}
        >
          Download MIDI
        </a>
      </div>
    </div>
  );
}
