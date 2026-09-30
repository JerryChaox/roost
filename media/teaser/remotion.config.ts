import {Config} from '@remotion/cli/config';

// Render settings live in the package.json `render:*` scripts.
Config.setEntryPoint('./src/index.ts');
Config.setOverwriteOutput(true);
