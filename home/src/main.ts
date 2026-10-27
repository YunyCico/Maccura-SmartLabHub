import { createApp } from 'vue'
import './style.css'
import App from './App.vue'
import router from './router'
import scrollLimit from './directives/scrollLimit'

const app = createApp(App)
app.use(router)
// 列表区域最多显示 6 条，其余通过滚轮向下查看
app.directive('scroll-limit', scrollLimit)
app.mount('#app')
