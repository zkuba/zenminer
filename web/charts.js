// charts.js — minimal T-Rex-like chart module

(function(){
  window.ZEN_CHARTS = (function(){
    let chart = null;
    let labels = [];
    let data = [];
    const MAX_POINTS = 60;

    function init(){
      const canvas = document.getElementById('hr-chart');
      if(!canvas) return false;
      const ctx = canvas.getContext('2d');

      chart = new Chart(ctx, {
        type: 'line',
        data: {
          labels: labels,
          datasets: [{
            label: 'Hashrate',
            data: data,
            borderWidth: 2,
            tension: 0.2,
            borderColor: '#2ecc71',
            fill: false,
          }]
        },
        options: {
          animation: false,
          responsive: true,
          scales: {
            y: {
              ticks: {
                callback: function(value){
                  if(value >= 1e12) return (value/1e12).toFixed(1)+'P';
                  if(value >= 1e9) return (value/1e9).toFixed(1)+'T';
                  if(value >= 1e6) return (value/1e6).toFixed(1)+'G';
                  if(value >= 1e3) return (value/1e3).toFixed(1)+'M';
                  return value;
                }
              }
            }
          },
          plugins:{ legend:{display:false} }
        }
      });
      return true;
    }

    function addPoint(v){
      const t = new Date().toLocaleTimeString();
      labels.push(t);
      data.push(v || 0);
      if(labels.length > MAX_POINTS){
        labels.shift();
        data.shift();
      }
      if(chart) chart.update('none');
    }

    window.addEventListener('load', ()=>{ try{ init(); }catch(e){ console.warn("charts init failed",e); } });
    return { addPoint, init };
  })();
})();
