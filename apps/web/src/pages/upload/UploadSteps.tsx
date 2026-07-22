import { Check } from 'lucide-react'

const labels = ['选择范围', '添加文件', '核对并更新']

export default function UploadSteps({ current }: { current: number }) {
  return <ol className="steps" aria-label="添加本月数据步骤">
    {labels.map((label, index) => <li key={label} className={current === index + 1 ? 'active' : current > index + 1 ? 'done' : ''}><span>{current > index + 1 ? <Check /> : index + 1}</span>{label}</li>)}
  </ol>
}
