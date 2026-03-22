/**
 * Temporary store for pending upload data.
 * Set on Home.vue before routing to Process.vue, cleared after ontology API call.
 */
import { reactive } from 'vue'

const state = reactive({
  files: [],
  simulationRequirement: '',
  isPending: false,
  userLlmApiKey: '',
  userZepApiKey: '',
  userLlmModelName: '',
})

export function setPendingUpload(files, requirement, keys = {}) {
  state.files = files
  state.simulationRequirement = requirement
  state.isPending = true
  state.userLlmApiKey = keys.userLlmApiKey || ''
  state.userZepApiKey = keys.userZepApiKey || ''
  state.userLlmModelName = keys.userLlmModelName || ''
}

export function getPendingUpload() {
  return {
    files: state.files,
    simulationRequirement: state.simulationRequirement,
    isPending: state.isPending,
    userLlmApiKey: state.userLlmApiKey,
    userZepApiKey: state.userZepApiKey,
    userLlmModelName: state.userLlmModelName,
  }
}

export function clearPendingUpload() {
  state.files = []
  state.simulationRequirement = ''
  state.isPending = false
  state.userLlmApiKey = ''
  state.userZepApiKey = ''
  state.userLlmModelName = ''
}

export default state
