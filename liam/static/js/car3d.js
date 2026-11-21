/* LiAuto-Mate 3D 车模（概览车辆状态卡）：
   程序化风格化 L 系 SUV（白漆 = 用户实车 White L7 Air）。
   - OrbitControls 拖拽旋转/缩放；空闲 8s 后自动缓慢旋转
   - 状态联动：门开→车门摆开；充电→充电口呼吸；未锁→车底琥珀警戒环；
     哨兵→车顶绿点；行驶→车轮按速度滚动
   - WebGL 不可用时页面回退到顶视 SVG 线稿（car-fallback）
   依赖 /static/vendor/three/（本地 vendored，离线可用）。 */

import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';
import { RoundedBoxGeometry } from 'three/addons/geometries/RoundedBoxGeometry.js';

export function initCar3D(container, state) {
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  container.appendChild(renderer.domElement);

  const scene = new THREE.Scene();
  const pmrem = new THREE.PMREMGenerator(renderer);
  scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;

  const camera = new THREE.PerspectiveCamera(34, 1, 0.1, 60);
  const view = new URLSearchParams(location.search).get('view');
  camera.position.set(5.0, 1.6, 4.3);
  if (view === 'side') camera.position.set(0.3, 1.15, 7.4);
  const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const allowRotation = view !== 'side' && !reduced;

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.target.set(0, 0.9, 0);
  controls.enablePan = false;
  controls.minDistance = 3.6;
  controls.maxDistance = 10;
  controls.maxPolarAngle = 1.42;
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.autoRotate = allowRotation;
  controls.autoRotateSpeed = 0.7;
  let idleTimer = null;
  controls.addEventListener('start', () => {
    controls.autoRotate = false;
    clearTimeout(idleTimer);
  });
  controls.addEventListener('end', () => {
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => { controls.autoRotate = allowRotation; }, 8000);
  });

  // ---- 灯光 ----
  const key = new THREE.DirectionalLight(0xffffff, 1.1);
  key.position.set(4, 6, 3);
  key.castShadow = true;
  key.shadow.mapSize.set(1024, 1024);
  key.shadow.camera.left = -4; key.shadow.camera.right = 4;
  key.shadow.camera.top = 4; key.shadow.camera.bottom = -4;
  scene.add(key);
  const rim = new THREE.DirectionalLight(0x8fb7ff, 0.5);
  rim.position.set(-5, 3, -4);
  scene.add(rim);
  scene.add(new THREE.HemisphereLight(0xcfd8e8, 0x11131a, 0.35));

  // ---- 材质 ----
  const paint = new THREE.MeshPhysicalMaterial({
    color: 0xe9ecf1, metalness: 0.55, roughness: 0.32, clearcoat: 0.7,
    clearcoatRoughness: 0.25});
  const glass = new THREE.MeshPhysicalMaterial({
    color: 0x0b0e14, metalness: 0.9, roughness: 0.08});
  const trim = new THREE.MeshStandardMaterial({
    color: 0x14161c, metalness: 0.4, roughness: 0.6});
  const tireMat = new THREE.MeshStandardMaterial({
    color: 0x0a0b0d, roughness: 0.95});
  const rimMat = new THREE.MeshStandardMaterial({
    color: 0x9aa3b2, metalness: 0.9, roughness: 0.25});

  const car = new THREE.Group();
  scene.add(car);

  const add = (geo, mat, x, y, z, shadow = true) => {
    const m = new THREE.Mesh(geo, mat);
    m.position.set(x, y, z);
    if (shadow) m.castShadow = true;
    car.add(m);
    return m;
  };

  // ---- 车身（x 轴=车长，+x 车头）----
  // 贝塞尔侧影一次挤出成型：前杠→引擎盖→A 柱→平顶→D 柱→尾门连续轮廓
  //（替代盒子拼装——9 版实测盒子拼不出稳定 SUV 比例）
  const prof = new THREE.Shape();
  prof.moveTo(-2.35, 0.28);
  prof.lineTo(2.35, 0.28);
  prof.quadraticCurveTo(2.47, 0.28, 2.47, 0.52);
  prof.lineTo(2.46, 0.82);
  prof.quadraticCurveTo(2.44, 0.94, 2.30, 0.98);
  prof.lineTo(2.05, 1.03);
  prof.quadraticCurveTo(1.60, 1.14, 1.35, 1.28);
  prof.quadraticCurveTo(1.05, 1.42, 0.82, 1.54);
  prof.lineTo(0.70, 1.57);
  prof.quadraticCurveTo(0.50, 1.62, 0.00, 1.62);
  prof.lineTo(-1.70, 1.60);
  prof.quadraticCurveTo(-2.10, 1.56, -2.28, 1.30);
  prof.lineTo(-2.33, 0.95);
  prof.quadraticCurveTo(-2.37, 0.50, -2.35, 0.28);
  const bodyGeo = new THREE.ExtrudeGeometry(prof, {
    depth: 1.78, bevelEnabled: true, bevelThickness: 0.07,
    bevelSize: 0.07, bevelSegments: 4, curveSegments: 24});
  bodyGeo.translate(0, 0, -0.89);
  add(bodyGeo, paint, 0, 0, 0);
  // 底裙（深色包裹一圈，SUV 厚重感）
  add(new RoundedBoxGeometry(4.72, 0.34, 1.90, 4, 0.10), trim, 0, 0.36, 0);
  // ---- 贴面玻璃窗 ----
  const win = (w, h, x, y, z, ry = 0, rz = 0) => {
    const m = new THREE.Mesh(
      new RoundedBoxGeometry(w, h, 0.04, 2, 0.03), glass);
    m.position.set(x, y, z);
    m.rotation.y = ry; m.rotation.z = rz;
    car.add(m);
    return m;
  };
  // 前挡（引擎盖顶直连车顶：薄沿 x、长沿斜面、宽沿 z——勿用 win() 侧贴参数）
  const ws = new THREE.Mesh(
    new RoundedBoxGeometry(0.04, 0.62, 1.48, 2, 0.02), glass);
  ws.position.set(1.08, 1.41, 0);
  ws.rotation.z = 0.46;   // A 柱斜面：恰好跨引擎盖顶到车顶，勿穿顶
  car.add(ws);
  // 两侧侧窗（前/后各一块，贴上车体侧面）
  win(0.95, 0.33, 0.25, 1.38, 0.965); win(1.55, 0.33, -1.20, 1.38, 0.965);
  win(0.95, 0.33, 0.25, 1.38, -0.965); win(1.55, 0.33, -1.20, 1.38, -0.965);
  // 后挡（尾门贴面，薄板转向 -x）
  win(1.45, 0.33, -2.40, 1.38, 0, Math.PI / 2);
  // 车顶行李轨
  add(new RoundedBoxGeometry(2.0, 0.05, 0.06, 2, 0.02), trim, -0.8, 1.68, 0.64, false);
  add(new RoundedBoxGeometry(2.0, 0.05, 0.06, 2, 0.02), trim, -0.8, 1.68, -0.64, false);
  // 大灯（贯穿式前灯带）
  const lampMat = new THREE.MeshStandardMaterial({
    color: 0x223040, emissive: 0xcfe8ff, emissiveIntensity: 1.4});
  add(new RoundedBoxGeometry(0.09, 0.06, 1.60, 2, 0.03), lampMat, 2.44, 0.88, 0, false);
  // 贯穿式尾灯
  const tailMat = new THREE.MeshStandardMaterial({
    color: 0x2a0808, emissive: 0xff2d2d, emissiveIntensity: 1.2});
  add(new RoundedBoxGeometry(0.08, 0.10, 1.80, 2, 0.03), tailMat, -2.39, 0.95, 0, false);

  // ---- 四轮（轮胎+轮毂，行驶时滚动）----
  const wheels = [];
  const tireGeo = new THREE.CylinderGeometry(0.40, 0.40, 0.32, 28);
  const rimGeo = new THREE.CylinderGeometry(0.24, 0.24, 0.33, 18);
  for (const [x, z] of [[1.52, 0.99], [1.52, -0.99], [-1.52, 0.99], [-1.52, -0.99]]) {
    const g = new THREE.Group();
    const tire = new THREE.Mesh(tireGeo, tireMat);
    tire.rotation.x = Math.PI / 2;
    tire.castShadow = true;
    g.add(tire);
    const r = new THREE.Mesh(rimGeo, rimMat);
    r.rotation.x = Math.PI / 2;
    g.add(r);
    g.position.set(x, 0.42, z);
    car.add(g);
    wheels.push(g);
  }

  // ---- 车门（铰链组，开门摆出）----
  const doors = {};   // key → {group, target, cur}
  const doorGeo = new RoundedBoxGeometry(0.88, 0.60, 0.05, 3, 0.03);
  const mkDoor = (key, cx, zSide) => {
    const hinge = new THREE.Group();
    const m = new THREE.Mesh(doorGeo, paint);
    m.castShadow = true;
    m.position.x = 0.44;                       // 铰链在门前缘
    hinge.add(m);
    hinge.position.set(cx, 0.95, zSide * 0.985);
    car.add(hinge);
    doors[key] = { g: hinge, target: 0, cur: 0, side: zSide };
  };
  mkDoor('main', 0.72, -1);      // 前左（主驾）
  mkDoor('copilot', 0.72, 1);    // 前右
  mkDoor('bl', -0.30, -1);      // 后左
  mkDoor('br', -0.30, 1);      // 后右

  // ---- 充电口（左后翼子板，充电呼吸）----
  const portMat = new THREE.MeshStandardMaterial({
    color: 0xe9ecf1, metalness: 0.5, roughness: 0.4,
    emissive: 0x3ecf8e, emissiveIntensity: 0});
  add(new RoundedBoxGeometry(0.05, 0.16, 0.16, 2, 0.02), portMat, -1.95, 0.92, -0.995, false);

  // ---- 哨兵点（车顶前缘）----
  const sentryMat = new THREE.MeshStandardMaterial({
    color: 0x0e1a12, emissive: 0x3ecf8e, emissiveIntensity: 0});
  add(new THREE.SphereGeometry(0.05, 16, 12), sentryMat, 0.5, 1.70, 0, false);

  // ---- 未锁警戒环（车底）----
  const ringMat = new THREE.MeshBasicMaterial({
    color: 0xf5b942, transparent: true, opacity: 0, side: THREE.DoubleSide});
  const ring = new THREE.Mesh(new THREE.RingGeometry(2.55, 2.68, 56), ringMat);
  ring.rotation.x = -Math.PI / 2;
  ring.position.y = 0.02;
  car.add(ring);

  // ---- 地面接影 ----
  const ground = new THREE.Mesh(
    new THREE.CircleGeometry(7, 48),
    new THREE.ShadowMaterial({ opacity: 0.42 }));
  ground.rotation.x = -Math.PI / 2;
  ground.receiveShadow = true;
  scene.add(ground);

  // ---- 尺寸 ----
  const resize = () => {
    const w = container.clientWidth, h = container.clientHeight;
    if (!w || !h) return;
    renderer.setSize(w, h);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  };
  resize();
  new ResizeObserver(resize).observe(container);

  // ---- 状态驱动 ----
  const st = { charging: false, unlocked: false, sentry: false, spin: 0 };

  function update(v) {
    const d = (v.doors || {});
    doors.main.target = Number(d.door_main) > 0 ? 0.95 : 0;
    doors.copilot.target = Number(d.door_copilot) > 0 ? -0.95 : 0;
    doors.bl.target = Number(d.door_bl) > 0 ? 0.95 : 0;
    doors.br.target = Number(d.door_br) > 0 ? -0.95 : 0;
    st.charging = v.state === 'charging';
    st.unlocked = d.main_locked != null && Number(d.main_locked) === 0;
    st.sentry = Boolean(Number(v.sentry));
    const spd = (v.loc && v.loc.spd) || 0;
    st.spin = v.state === 'driving' ? Math.max(spd, 5) / 30 : 0;
  }
  if (state) update(state);

  // ---- 渲染循环 ----
  const clock = new THREE.Clock();
  renderer.setAnimationLoop(() => {
    const t = reduced ? 0 : clock.getElapsedTime();
    for (const k in doors) {
      const d = doors[k];
      d.cur = reduced ? d.target : d.cur + (d.target - d.cur) * 0.08;
      d.g.rotation.y = d.cur * d.side;
    }
    portMat.emissiveIntensity = st.charging ? (1.1 + Math.sin(t * 2.6) * 0.9) : 0;
    sentryMat.emissiveIntensity = st.sentry ? 1.8 : 0;
    ringMat.opacity = st.unlocked ? 0.18 + 0.14 * Math.sin(t * 2.2) : 0;
    for (const w of wheels) w.children.forEach(
      m => m.rotation.y += reduced ? 0 : st.spin * 0.25);
    controls.update();
    renderer.render(scene, camera);
  });

  return { update };
}
