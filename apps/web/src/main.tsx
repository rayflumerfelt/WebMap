import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import { App } from './App.js';
import './styles/layout.css';

const container = document.getElementById('root');
if (!container) {
  throw new Error('No #root element. index.html and main.tsx are out of step.');
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
