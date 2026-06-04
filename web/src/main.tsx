import gsap from 'gsap';
import { useGSAP } from '@gsap/react';
import ReactDOM from 'react-dom/client';
import App from './App';
import './styles.css';

// 注册 GSAP React 插件，保证 useGSAP 在全局入口完成初始化。
gsap.registerPlugin(useGSAP);

// React 应用入口，挂载 A 股数据工作台。
ReactDOM.createRoot(document.getElementById('root') as HTMLElement).render(<App />);
