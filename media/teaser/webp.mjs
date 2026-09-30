// Usage: node webp.mjs <in.mp4> <out.webp> [quality=80] [width=960] [mode=lossy|lossless|mixed|near<0-100>]
// Animated WebP at the source 30 fps via libwebp's img2webp (Homebrew's ffmpeg
// ships without the libwebp encoder). WebP frame delays are whole milliseconds,
// so durations cycle 33/33/34 ms to average exactly 30 fps.
import {execFileSync} from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const [input, output, quality = '80', width = '960', mode = 'lossy'] = process.argv.slice(2);
if (!input || !output) {
  console.error('usage: node webp.mjs <in.mp4> <out.webp> [quality=80] [width=960]');
  process.exit(1);
}

const tmp = fs.mkdtempSync(path.join(path.dirname(output), '.webp-frames-'));
try {
  execFileSync(
    'ffmpeg',
    ['-v', 'error', '-y', '-i', input, '-vf', `fps=30,scale=${width}:-1:flags=lanczos`, path.join(tmp, '%04d.png')],
    {stdio: 'inherit'},
  );
  const frames = fs.readdirSync(tmp).filter((f) => f.endsWith('.png')).sort();
  const modeArgs = mode.startsWith('near')
    ? ['-near_lossless', mode.slice(4), '-lossless'] // e.g. near60
    : mode === 'mixed'
      ? ['-mixed']
      : [`-${mode}`];
  const args = ['-loop', '0', ...modeArgs, '-q', quality, '-m', '6'];
  let last = 0;
  frames.forEach((f, i) => {
    const d = i % 3 === 2 ? 34 : 33;
    if (d !== last) args.push('-d', String(d));
    last = d;
    args.push(path.join(tmp, f));
  });
  args.push('-o', output);
  // img2webp tokenizes its arguments from a file when given a single file name.
  const argFile = path.join(tmp, 'args.txt');
  fs.writeFileSync(argFile, args.join('\n'));
  execFileSync('img2webp', [argFile], {stdio: ['ignore', 'ignore', 'inherit']});
  console.log(`${output}: ${frames.length} frames, ${fs.statSync(output).size} bytes`);
} finally {
  fs.rmSync(tmp, {recursive: true, force: true});
}
