const assert = require('node:assert/strict');
const timeline = require('./web/timeline.js');
const cues = [{start:0.2,end:0.6}, {start:0.8,end:1.3}, {start:2,end:2.4}];
assert.equal(timeline.cueAt(cues, 0), null);
assert.equal(timeline.cueAt(cues, 0.2), cues[0]);
assert.equal(timeline.cueAt(cues, 0.6), null); // No highlight during a pause.
assert.equal(timeline.cueAt(cues, 2.1), cues[2]); // Seek forward.
assert.equal(timeline.cueAt(cues, 0.4), cues[0]); // Seek backward.
assert.equal(timeline.cueAt(cues, 2.4), null);
assert.equal(timeline.cueAt(cues, NaN), null);
assert.equal(timeline.overlaps(4, 8, 8, 12), false);
assert.equal(timeline.overlaps(4, 8, 5, 7), true);
assert.deepEqual(timeline.tokenize('🚀 Hello world.').map(word => [word.text,word.start,word.end]), [['🚀',0,1],['Hello',2,7],['world.',8,14]]);
console.log('Playback-clock lookup, seeks, pauses, and source ranges passed.');
