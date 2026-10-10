// Look up measured cues using the audio element's clock, including after seeks.
const TovoiceTimeline = {
  cueAt(cues, time) {
    if (!Number.isFinite(time) || time < 0) return null;
    let low = 0, high = cues.length;
    while (low < high) {
      const mid = (low + high) >>> 1;
      if (cues[mid].start <= time) low = mid + 1;
      else high = mid;
    }
    const cue = cues[low - 1];
    return cue && time < cue.end ? cue : null;
  },
  overlaps(start, end, rangeStart, rangeEnd) {
    return start < rangeEnd && end > rangeStart;
  },
  tokenize(text) {
    let cursor = 0, points = 0;
    const words = [];
    for (const match of text.matchAll(/\S+/g)) {
      const before = text.slice(cursor, match.index);
      const start = points + Array.from(before).length;
      const end = start + Array.from(match[0]).length;
      cursor = match.index + match[0].length;
      points = end;
      words.push({text:match[0], before, start, end, offsetEnd:cursor});
    }
    return words;
  },
};
if (typeof module !== 'undefined') module.exports = TovoiceTimeline;
