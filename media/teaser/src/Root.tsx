import React from 'react';
import {Composition} from 'remotion';
import {Teaser, TOTAL_FRAMES} from './Teaser';

export const RemotionRoot: React.FC = () => (
  <Composition
    id="RoostTeaser"
    component={Teaser}
    durationInFrames={TOTAL_FRAMES}
    fps={30}
    width={1280}
    height={720}
  />
);
