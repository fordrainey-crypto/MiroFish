import service, { requestWithRetry } from './index'

/**
 * 开始报告生成
 * @param {Object} data - { simulation_id, force_regenerate? }
 */
export const generateReport = (data) => {
  const keys = {
    user_llm_api_key: localStorage.getItem('mf_llm_key') || undefined,
    user_zep_api_key: localStorage.getItem('mf_zep_key') || undefined,
    user_llm_model_name: localStorage.getItem('mf_model') || undefined,
  }
  return requestWithRetry(() => service.post('/api/report/generate', { ...data, ...keys }), 3, 1000)
}

/**
 * 获取报告生成状态
 * @param {string} reportId
 */
export const getReportStatus = (reportId) => {
  return service.get(`/api/report/generate/status`, { params: { report_id: reportId } })
}

/**
 * 获取 Agent 日志（增量）
 * @param {string} reportId
 * @param {number} fromLine - 从第几行开始获取
 */
export const getAgentLog = (reportId, fromLine = 0) => {
  return service.get(`/api/report/${reportId}/agent-log`, { params: { from_line: fromLine } })
}

/**
 * 获取控制台日志（增量）
 * @param {string} reportId
 * @param {number} fromLine - 从第几行开始获取
 */
export const getConsoleLog = (reportId, fromLine = 0) => {
  return service.get(`/api/report/${reportId}/console-log`, { params: { from_line: fromLine } })
}

/**
 * 获取报告详情
 * @param {string} reportId
 */
export const getReport = (reportId) => {
  return service.get(`/api/report/${reportId}`)
}

/**
 * 与 Report Agent 对话
 * @param {Object} data - { simulation_id, message, chat_history? }
 */
export const chatWithReport = (data) => {
  const keys = {
    user_llm_api_key: localStorage.getItem('mf_llm_key') || undefined,
    user_zep_api_key: localStorage.getItem('mf_zep_key') || undefined,
    user_llm_model_name: localStorage.getItem('mf_model') || undefined,
  }
  return requestWithRetry(() => service.post('/api/report/chat', { ...data, ...keys }), 3, 1000)
}

/**
 * Generate a plain-English summary of a report
 * @param {string} reportId
 */
export const simplifyReport = (reportId) => {
  return service.post(`/api/report/${reportId}/simplify`)
}
