import {createRoot} from 'react-dom/client';
import {App} from './App';
import './style.css';

// Kept so existing imports of `App` from './main' keep working.
export {App};

const root = document.getElementById('root');
if (root) createRoot(root).render(<App />);
