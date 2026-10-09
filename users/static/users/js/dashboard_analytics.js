function toggleDebug(button){
    const el = document.getElementById('results-desc');
    if(!el) return;
    el.hidden = !el.hidden;
    button.setAttribute('aria-expanded', String(!el.hidden));
    button.textContent = el.hidden ? 'Show SQL' : 'Hide SQL';
  }

  UIComponents.tabs(document.getElementById("analytics-tabs"));

  // Chart placeholders
  (function initCharts(){
    if (typeof Chart === 'undefined') return;
    const line = document.getElementById('lineChart');
    const bar = document.getElementById('barChart');
    const scatter = document.getElementById('scatterChart');
    if(line){
      new Chart(line.getContext('2d'), {
        type:'line',
        data:{ labels:['Mon','Tue','Wed','Thu','Fri','Sat','Sun'], datasets:[{label:'Sessions', data:[120,150,100,170,190,230,210], fill:false, tension:0.3, pointRadius:4, borderWidth:2}]},
        options:{ responsive:true, maintainAspectRatio:false, plugins:{legend:{display:false}}, scales:{x:{grid:{display:false}}} }
      });
    }
    if(bar){
      new Chart(bar.getContext('2d'), {
        type:'bar',
        data:{ labels:['Website','WhatsApp','Messenger','SMS','Other'], datasets:[{label:'Events', data:[450,320,210,90,35], borderWidth:1}]},
        options:{ responsive:true, maintainAspectRatio:false, plugins:{legend:{display:false}}, scales:{y:{beginAtZero:true}}}
      });
    }
    if(scatter){
      new Chart(scatter.getContext('2d'), {
        type:'scatter',
        data:{ datasets:[{ label:'Sessions', data:[{x:5,y:12},{x:1,y:3},{x:12,y:50},{x:7,y:20},{x:3,y:8},{x:9,y:30}], pointRadius:6 }]},
        options:{ responsive:true, maintainAspectRatio:false, scales:{ x:{ title:{display:true,text:'Messages'} }, y:{ title:{display:true,text:'Duration (seconds)'} } } }
      });
    }
  })();

document.querySelectorAll('[data-toggle-debug]').forEach(button => {
  button.addEventListener('click', () => toggleDebug(button));
});
