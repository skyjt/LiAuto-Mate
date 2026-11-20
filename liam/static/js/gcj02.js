/* WGS-84 → GCJ-02（公开算法，展示层专用；库内永远存 WGS-84 —— 方案 §8.1.1） */
function wgs2gcj(lat, lon) {
  const a = 6378245.0;
  const ee = 0.00669342162296594323;
  function tLat(x, y) {
    let r = -100.0 + 2.0*x + 3.0*y + 0.2*y*y + 0.1*x*y + 0.2*Math.sqrt(Math.abs(x));
    r += (20.0*Math.sin(6.0*x*Math.PI) + 20.0*Math.sin(2.0*x*Math.PI)) * 2.0/3.0;
    r += (20.0*Math.sin(y*Math.PI) + 40.0*Math.sin(y/3.0*Math.PI)) * 2.0/3.0;
    r += (160.0*Math.sin(y/12.0*Math.PI) + 320.0*Math.sin(y*Math.PI/30.0)) * 2.0/3.0;
    return r;
  }
  function tLon(x, y) {
    let r = 300.0 + x + 2.0*y + 0.1*x*x + 0.1*x*y + 0.1*Math.sqrt(Math.abs(x));
    r += (20.0*Math.sin(x*Math.PI) + 40.0*Math.sin(x/3.0*Math.PI)) * 2.0/3.0;
    r += (150.0*Math.sin(x/12.0*Math.PI) + 300.0*Math.sin(x/30.0*Math.PI)) * 2.0/3.0;
    return r;
  }
  let dLat = tLat(lon - 105.0, lat - 35.0);
  let dLon = tLon(lon - 105.0, lat - 35.0);
  const radLat = lat / 180.0 * Math.PI;
  let magic = Math.sin(radLat);
  magic = 1 - ee * magic * magic;
  const sq = Math.sqrt(magic);
  dLat = (dLat * 180.0) / ((a * (1 - ee)) / (magic * sq) * Math.PI);
  dLon = (dLon * 180.0) / (a / sq * Math.cos(radLat) * Math.PI);
  return [lat + dLat, lon + dLon];
}
