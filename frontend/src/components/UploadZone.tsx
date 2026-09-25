import { useState, useRef, useCallback } from 'react';
import { uploadScore } from '../api';
import type { ScoreRead } from '../types';

interface UploadZoneProps {
  onUploaded: (score: ScoreRead) => void;
}

export default function UploadZone({ onUploaded }: UploadZoneProps) {
  const [isDragging, setIsDragging] = useState(false);
  const [isUploading, setIsUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [ocrEnabled, setOcrEnabled] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const handleFiles = useCallback(
    async (files: File[]) => {
      if (files.length === 0) return;
      setError(null);
      setIsUploading(true);
      try {
        // Sorted by filename so page order is predictable regardless of the
        // order the OS/browser reports selected or dropped files in.
        const sorted = [...files].sort((a, b) => a.name.localeCompare(b.name));
        const score = await uploadScore(sorted, ocrEnabled);
        onUploaded(score);
      } catch (err: unknown) {
        const message =
          err instanceof Error ? err.message : 'Upload failed. Please try again.';
        setError(message);
      } finally {
        setIsUploading(false);
      }
    },
    [onUploaded, ocrEnabled]
  );

  const handleDragOver = useCallback((e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setIsDragging(true);
  }, []);

  const handleDragLeave = useCallback((e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setIsDragging(false);
  }, []);

  const handleDrop = useCallback(
    (e: React.DragEvent<HTMLDivElement>) => {
      e.preventDefault();
      setIsDragging(false);
      const files = Array.from(e.dataTransfer.files);
      if (files.length > 0) {
        handleFiles(files);
      }
    },
    [handleFiles]
  );

  const handleClick = useCallback(() => {
    if (!isUploading) {
      inputRef.current?.click();
    }
  }, [isUploading]);

  const handleInputChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const files = Array.from(e.target.files ?? []);
      if (files.length > 0) {
        handleFiles(files);
        // Reset so the same file(s) can be re-uploaded
        e.target.value = '';
      }
    },
    [handleFiles]
  );

  const zoneStyle: React.CSSProperties = {
    border: `2px dashed ${isDragging ? '#4a90e2' : '#aaa'}`,
    borderRadius: '12px',
    padding: '48px 32px',
    textAlign: 'center',
    cursor: isUploading ? 'not-allowed' : 'pointer',
    backgroundColor: isDragging ? '#eef4ff' : '#fafafa',
    transition: 'all 0.2s ease',
    userSelect: 'none',
  };

  return (
    <div style={{ maxWidth: '600px', margin: '0 auto 32px' }}>
      <div
        style={zoneStyle}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
        onClick={handleClick}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => e.key === 'Enter' && handleClick()}
        aria-label="Upload sheet music"
      >
        <input
          ref={inputRef}
          type="file"
          multiple
          accept=".png,.jpg,.jpeg,.tiff,.tif,.pdf"
          style={{ display: 'none' }}
          onChange={handleInputChange}
        />
        <div style={{ fontSize: '48px', marginBottom: '12px' }}>🎼</div>
        {isUploading ? (
          <p style={{ color: '#555', margin: 0 }}>Uploading...</p>
        ) : (
          <p style={{ color: '#555', margin: 0 }}>
            Drop sheet music here or click to upload
          </p>
        )}
        <p style={{ color: '#999', fontSize: '13px', marginTop: '8px', marginBottom: 0 }}>
          Accepted: PNG, JPG, JPEG, TIFF, PDF — mehrere Dateien werden zu einem
          Stück zusammengefügt (Seitenreihenfolge nach Dateiname)
        </p>
      </div>
      <label
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: '8px',
          marginTop: '12px',
          cursor: isUploading ? 'not-allowed' : 'pointer',
          userSelect: 'none',
          fontSize: '14px',
          color: '#444',
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <input
          type="checkbox"
          checked={ocrEnabled}
          disabled={isUploading}
          onChange={(e) => setOcrEnabled(e.target.checked)}
          style={{ width: '16px', height: '16px', cursor: isUploading ? 'not-allowed' : 'pointer' }}
        />
        Liedtexte erkennen (OCR)
      </label>
      {error && (
        <div
          style={{
            marginTop: '12px',
            padding: '10px 14px',
            backgroundColor: '#fff0f0',
            border: '1px solid #ffcccc',
            borderRadius: '6px',
            color: '#cc0000',
            fontSize: '14px',
          }}
        >
          {error}
        </div>
      )}
    </div>
  );
}
